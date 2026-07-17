# ABOUTME: Workflow-side utilities for building agent workflows that integrate
# with AgentClient. Provides turn lifecycle management, tool execution with
# automatic event publishing, and the update/query/signal handlers that the
# AgentClient protocol requires.
#
# Usage: declare accepted messages as ``@agent.accepts`` handler methods
# (``async def name(self, msg: InputModel) -> OutputModel``); construct an
# AgentWorkflowRunner(config, stream=..., approval_policy_default=...) in your
# @workflow.init; and in @workflow.run drive the turn loop with ``await runner.run(self)``.
# The runner discovers the handlers, routes
# each inbound ``send_agent_message`` envelope to the one its ``type`` names, validates the
# payload into that handler's input model, publishes the handler's return value as the
# reply, and announces the callable surface via the ``agent_interface`` query.

from __future__ import annotations

import ast
import contextvars
import inspect
import textwrap
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import (
    Annotated,
    Any,
    Literal,
    ParamSpec,
    TypeVar,
    cast,
    get_type_hints,
    overload,
)

from pydantic import BaseModel, TypeAdapter, ValidationError
from temporalio import activity, workflow
from temporalio.contrib.workflow_streams import (
    TopicHandle,
    WorkflowStream,
    WorkflowStreamClient,
    WorkflowTopicHandle,
)
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness.agent_protocol import (
    AGENT_ID_LENGTH,
    AGENT_INTERFACE_QUERY,
    AGENT_STATUS_QUERY,
    EXECUTE_OPERATOR_COMMAND_UPDATE,
    OPERATOR_INTERFACE_QUERY,
    PROVIDE_CALLBACK_RESULT_UPDATE,
    DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT,
    DEFAULT_SUBAGENT_START_TO_CLOSE_TIMEOUT,
    RUN_SUBAGENT_TURN_ACTIVITY,
    SEND_AGENT_MESSAGE_UPDATE,
    SET_ENABLED_MCP_SERVERS_UPDATE,
    TOOL_APPROVAL_UPDATE,
    TURN_EVENTS_TOPIC,
    AcceptedFunction,
    AgentConfig,
    AgentError,
    AgentEvent,
    AgentMessage,
    AgentReply,
    AgentStatus,
    AgentStreamItem,
    CallbackRequested,
    CallbackResolved,
    CallbackResult,
    CallbackResultAck,
    MessageQueued,
    OperatorCommand,
    OperatorCommandCompleted,
    OperatorCommandFailed,
    OperatorCommandRequest,
    OperatorCommandResult,
    OperatorCommandStarted,
    PendingApproval,
    PendingCallback,
    PendingTurn,
    RunSubagentTurnInput,
    SlashCommand,
    SubagentInfo,
    SubagentReplyReceived,
    SubagentStarted,
    SubagentStopped,
    SubagentTurnResult,
    ToolApprovalContext,
    ToolApprovalDecision,
    ToolApprovalPolicy,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolApprovalResult,
    ToolEndEvent,
    ToolErrorEvent,
    ToolStartEvent,
    TextReply,
    TurnEnded,
    TurnStarted,
    AgentMessageReply,
)
from temporal_agent_harness.harness.slash_commands import (
    SlashCommandContext,
    SlashCommandDefinition,
    default_commands,
)

# TurnStreamContext (the activity-side stream-publishing carrier this runner builds + consumes)
# lives in its own leaf module so the sandbox-safe activity contracts in agent_protocol can embed
# it without a circular import back through this module.
from temporal_agent_harness.harness.stream_context import TurnStreamContext

# ParamSpec/return-type vars for the tool decorators. They let each be typed as an
# identity over the wrapped callable (``Callable[P, Awaitable[R]] -> Callable[P,
# Awaitable[R]]``), so a decorated tool's call signature stays byte-for-byte the
# developer's own — full editor type-checking, the decorator invisible at the type
# level. The injected ``tool_ctx`` lives only on the activity body's runtime
# ``__signature__`` (for ``@activity.defn``), never in this static type.
_P = ParamSpec("_P")
_R = TypeVar("_R")

# The agent workflow class decorated by ``defn`` — bound to ``type`` and preserved
# through the decorator so the decorated class keeps its concrete type for callers.
_WorkflowClass = TypeVar("_WorkflowClass", bound=type)


# ---------------------------------------------------------------------------
# Injected[...] — mark a tool parameter as workflow-supplied (hidden from the model)
# ---------------------------------------------------------------------------


class _InjectedMarker:
    """Sentinel placed in an ``Annotated`` to tag a tool parameter as injected."""


_INJECTED = _InjectedMarker()

# A developer-supplied custom approval predicate — the FINAL approval layer, consulted
# only when the serializable ``ToolApprovalPolicy`` did not already auto-approve the call.
# Returns True to auto-approve (skip the human gate), False to fall through to gating.
# Passed to the runner via its ``custom_approval_fallback=`` constructor arg; it is
# non-serializable (a closure), so it is never carried in ``AgentConfig`` or status.
CustomApprovalFallback = Callable[[ToolApprovalContext], bool]

_SLASH_MESSAGE_TYPE = "slash"

_InjectedT = TypeVar("_InjectedT")

# Annotate a tool parameter ``x: Injected[Foo]`` to have the *workflow* supply it per
# call (via ``run_tool(injections=...)``) rather than the model. Such parameters are
# hidden from the model's tool schema and filled at dispatch. Statically it's just
# ``Foo`` (``Annotated`` metadata is invisible to type checkers and to pydantic), so the
# tool body sees the unwrapped type.
Injected = Annotated[_InjectedT, _INJECTED]


class ToolApprovalDenied(Exception):
    """Raised in-workflow when a gated tool call is denied (or auto-denied because the
    agent closed while it was pending).

    Propagates out of the tool's in-workflow invocation and is caught by the agent
    loop's per-call error handling, which surfaces it to the model as an ``is_error``
    function result — so a denied tool does not run, yet the agent turn continues. The
    activity is never dispatched, so this never consumes the tool's execution timeout.
    """

    def __init__(self, tool_name: str, reason: str | None) -> None:
        self.tool_name = tool_name
        self.reason = reason
        detail = f": {reason}" if reason else ""
        super().__init__(f"tool {tool_name!r} was not approved{detail}")


class CallbackToolError(Exception):
    """Raised in-workflow when a callback tool call cannot produce a result — the fulfilling
    client reported an error, the wait timed out, or the agent closed while it was pending.

    Raised from :meth:`AgentWorkflowRunner.await_callback_result` (the generic callback body),
    so it propagates out through the tool's :func:`tool_defn` wrapper, which publishes a
    ``tool_error`` and surfaces it to the model as an ``is_error`` function result — so a failed
    callback does not run and does not crash the turn.
    """

    def __init__(self, tool_name: str, reason: str | None) -> None:
        self.tool_name = tool_name
        self.reason = reason
        detail = f": {reason}" if reason else ""
        super().__init__(f"callback tool {tool_name!r} failed{detail}")


def _injected_param_names(fn: Callable[..., Any]) -> tuple[str, ...]:
    """Names of ``fn``'s parameters annotated ``Injected[...]`` — i.e. supplied by the
    workflow and hidden from the model. Resolves string annotations, so it works under
    ``from __future__ import annotations`` too."""
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = getattr(fn, "__annotations__", {})
    return tuple(
        name
        for name, hint in hints.items()
        if name != "return" and _INJECTED in getattr(hint, "__metadata__", ())
    )


# ---------------------------------------------------------------------------
# Ambient per-run / per-tool-call context (sealed inside the harness)
# ---------------------------------------------------------------------------
#
# Two module-level ContextVars carry state the tool machinery needs without the
# developer threading it through call sites. They are safe across concurrent
# workflows: each Temporal workflow instance runs its body as its own asyncio Task
# (its own copied ``contextvars.Context``), and an ``asyncio.gather`` child copies
# the parent context at creation — so a ``.set()`` in one workflow (or one tool
# call) can never bleed into another. NEITHER is touched by code outside the harness:
# they are set/reset only by ``run_tool`` and read only within this module (the
# workflow-tool path and ``AgentToolContext.for_current_tool_id``).

# Both are set together by ``run_tool``, scoped tightly around the single tool
# invocation (one synchronous frame, one ``await``), then reset — never held across
# the long-lived ``async with start()`` body, whose awaits Temporal's event loop runs
# in copied contexts that would defeat a Token.reset(). They are only ever READ during
# a tool call (by the workflow-tool path / the plugin's dispatch), which is exactly
# the window ``run_tool`` keeps them set, so this scope is sufficient.

# The active runner for the in-flight tool call — read to resolve the live turn stream
# context (``AgentToolContext.for_current_tool_id`` / workflow-tool publishing).
_CURRENT_RUNNER: contextvars.ContextVar[AgentWorkflowRunner | None] = (
    contextvars.ContextVar("agent_workflow_runner", default=None)
)

# The id of the tool call currently being executed. Per-invocation (so the same tool
# requested several times in one turn each gets its own id).
_CURRENT_TOOL_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_tool_id", default=None
)

# Workflow-supplied values for the in-flight tool call's *injected* parameters, keyed
# by parameter name. Set by ``run_tool`` and read at dispatch by the tool machinery
# (the ``activity_tool_defn`` dispatcher for activity tools, or the ``tool_defn`` path
# for inline tools) to fill parameters the caller injects rather than the model — see
# ``_current_tool_injections`` and ``Injected[...]``.
_CURRENT_TOOL_INJECTIONS: contextvars.ContextVar[Mapping[str, Any] | None] = (
    contextvars.ContextVar("agent_tool_injections", default=None)
)


def _current_tool_injections() -> Mapping[str, Any]:
    """The injected-parameter values for the in-flight tool call (empty if none).

    Read at tool dispatch to fill parameters the workflow supplies rather than the
    model (declared via ``Injected[...]``). Backed by the ambient
    ``_CURRENT_TOOL_INJECTIONS`` that ``run_tool`` sets for the duration of one call.
    """
    return _CURRENT_TOOL_INJECTIONS.get() or {}


def _current_tool_id() -> str:
    """Return the tool id of the in-flight tool call, or raise if there is none.

    Private harness helper backing :meth:`AgentToolContext.for_current_tool_id` — it
    reads the ambient ``_CURRENT_TOOL_ID`` that ``run_tool`` sets for the duration of
    one tool invocation. Raises if called outside a ``run_tool`` invocation (i.e. when
    no tool call is currently being dispatched).
    """
    tool_id = _CURRENT_TOOL_ID.get()
    if tool_id is None:
        raise RuntimeError(
            "no current tool id — _current_tool_id() must be called within a "
            "run_tool(...) invocation"
        )
    return tool_id


def _current_runner() -> AgentWorkflowRunner:
    """The runner executing the in-flight tool call, or raise if there is none.

    Private harness helper. Reads the ambient ``_CURRENT_RUNNER`` that ``run_tool`` parks for the
    duration of one tool invocation. The generated subagent tools (``subagent_toolset``) call
    this to reach the live runner — and thus its subagent registry + ``start_/stop_/
    run_subagent_turn`` methods — without a holder object or ``has_self`` plumbing (see the
    subagent-toolset design). Raises if called outside a ``run_tool(...)`` invocation."""
    runner = _CURRENT_RUNNER.get()
    if runner is None:
        raise RuntimeError(
            "no current runner — _current_runner() must be called within a "
            "run_tool(...) invocation"
        )
    return runner


async def _apply_approval_policy(
    tool_name: str, tool_input: dict[str, Any], *, inherently_safe: bool
) -> None:
    """Enforce the agent's tool-approval policy for the in-flight tool call.

    Runs IN-WORKFLOW at the top of every tool dispatch (never in the tool's activity).
    First consults the runner's live :class:`ToolApprovalPolicy` plus optional custom
    fallback via :meth:`AgentWorkflowRunner._auto_approves`:

      * auto-approved → returns immediately; the tool dispatches with no human gate.
      * otherwise → registers the call as PENDING (also exposed via the ``agent_status``
        query), publishes :class:`ToolApprovalRequested`, then waits — indefinitely, on a
        ``wait_condition`` so no activity timeout is consumed — for a ``tool_approval``
        decision, a *relaxing policy update* (which flips this entry to approved; see
        :meth:`AgentWorkflowRunner._apply_policy_update`), or agent close. On resolution it
        publishes :class:`ToolApprovalResolved` and, if denied (or auto-denied on close),
        raises :class:`ToolApprovalDenied`.

    Safe-by-default: with the baseline policy nothing is auto-approved, so every tool call
    is gated unless the policy (or fallback) opts it out. The tool's own
    ``inherently_safe`` claim is just an input to that decision — the *policy* decides.

    Concurrency: each gated call runs as its own asyncio task (the agent dispatches a
    turn's tool calls under ``asyncio.gather``), so many of these waits coexist, each
    keyed on its own ``tool_id``. Temporal re-evaluates every wait condition after each
    update, so whichever id is approved first unblocks immediately — independent of the
    order the calls were requested.
    """
    runner = _CURRENT_RUNNER.get()
    if runner is None:
        raise RuntimeError(
            f"tool {tool_name!r} has no active runner — it must be invoked via "
            f"run_tool within an active turn"
        )
    if runner._auto_approves(tool_name, tool_input, inherently_safe=inherently_safe):
        return  # policy (or custom fallback) approves — dispatch without gating.
    tool_id = _current_tool_id()
    ctx = runner.current_stream_context
    if ctx is None:
        raise RuntimeError(
            f"gated tool {tool_name!r} has no active turn to publish its approval against"
        )

    runner._status.register_pending_approval(
        tool_id,
        tool_name,
        tool_input,
        ctx.turn_number,
        ctx.turn_id,
        inherently_safe=inherently_safe,
    )
    runner._pub(
        ctx.turn_id,
        ctx.turn_number,
        ToolApprovalRequested(
            tool_id=tool_id, tool_name=tool_name, tool_input=tool_input
        ),
    )

    await workflow.wait_condition(
        lambda: runner._status.is_approval_resolved(tool_id) or runner._closed
    )

    # CAUSAL ORDERING: the ToolApprovalResolved event is published at the RESOLUTION SITE
    # (the ``tool_approval`` handler and the policy-update cascade), in the synchronous
    # order resolutions actually happen — so when one decision causes another (an
    # "approve & remember" allow-lists a tool and auto-resolves its sibling pending calls),
    # the causing call's resolution is published before the caused ones. It must NOT be
    # published here: many gates wake in the SAME workflow task and resume in registration
    # order, which need not match causal order. The ONE case the resolution site can't
    # cover is waking on agent close while still PENDING (no decision resolved it) — only
    # then does the gate finalize the auto-deny and publish it.
    if not runner._status.is_approval_resolved(tool_id):
        outcome = runner._status.finalize_approval(tool_id, closed=runner._closed)
        runner._publish_approval_resolved(tool_id)
    else:
        outcome = runner._status.finalize_approval(tool_id, closed=runner._closed)
    if not outcome.approved:
        raise ToolApprovalDenied(tool_name, outcome.reason)


def _render_message(message: AgentMessage) -> str:
    """Render an inbound message envelope to a display string for status/queue events.

    Compacts just the ``{type, payload}`` envelope content to JSON (the turn-protocol
    ``expected_turn`` is omitted — it's not part of the message) so the existing
    ``str``-typed event fields (``user_message``, ``PendingTurn.message``) stay a clean
    representation of the message. Consumers that want structure can parse it back.
    """
    return message.model_dump_json(include={"type", "payload"})


# ---------------------------------------------------------------------------
# Standardized agent-input contract enforcement
# ---------------------------------------------------------------------------
#
# Every agent that builds an AgentWorkflowRunner must declare its run/__init__ to take
# either nothing or a single AgentConfig. Standardizing the input is what lets harness
# agents be composed/substituted (top-level or sub-agent) knowing only AgentConfig. The
# check runs at runner construction time (inside the workflow's @workflow.init).


def _validate_agent_arg_types(workflow_name: str, arg_types: list[type] | None) -> None:
    """Raise ``TypeError`` unless ``arg_types`` is exactly ``[AgentConfig]``.

    Every agent must accept the single standardized config — no more, and not nothing.
    Pure (no introspection) so the wiring that finds the workflow's resolved argument
    types lives in :func:`_assert_standardized_agent_signature`.
    """
    types = list(arg_types or [])
    if types != [AgentConfig]:
        got = (
            ", ".join(getattr(t, "__name__", repr(t)) for t in types) or "no arguments"
        )
        raise TypeError(
            f"Agent workflow {workflow_name!r} violates the harness contract: its "
            f"run/__init__ must accept exactly one {AgentConfig.__name__} argument, but "
            f"accepts ({got}). A uniform input is what lets harness agents be substituted "
            f"as parent or sub-agents — configure agent-specific behavior at runtime "
            f"(e.g. slash commands) rather than via a custom input type."
        )


def _enclosing_workflow_class() -> type | None:
    """Walk the call stack for the nearest ``self`` that is a ``@workflow.defn`` class.

    Runner construction happens inside the workflow's ``@workflow.init``, so the
    workflow instance is an enclosing frame's ``self``; this recovers its class so the
    construction signature can be validated. Returns ``None`` if none is found.
    """
    frame: Any = inspect.currentframe()
    while frame is not None:
        candidate = frame.f_locals.get("self")
        if candidate is not None and workflow._Definition.from_class(type(candidate)):
            return type(candidate)
        frame = frame.f_back
    return None


def _assert_standardized_agent_signature() -> None:
    """Enforce the uniform agent-input contract for the workflow building this runner.

    Recovers the enclosing ``@workflow.defn`` class and validates its resolved run
    argument types (which Temporal keeps consistent with ``__init__``) against
    :func:`_validate_agent_arg_types`. Raises if no enclosing workflow is found, or if
    that workflow's signature breaks the contract.

    Only meaningful when actually constructing inside a workflow — which is the only
    way a real agent is built. When called outside one (e.g. an offline unit test that
    stubs the workflow handler registration), there's no agent contract to enforce, so
    it's a no-op.
    """
    if not workflow.in_workflow():
        return
    cls = _enclosing_workflow_class()
    if cls is None:
        raise RuntimeError(
            "AgentWorkflowRunner must be built inside an @workflow.defn class's "
            "@workflow.init; no enclosing workflow was found on the call stack."
        )
    defn = workflow._Definition.from_class(cls)
    assert defn is not None  # guaranteed by _enclosing_workflow_class's own check
    _validate_agent_arg_types(cls.__name__, defn.arg_types)


def _workflow_run_arg_types(cls: type) -> list[type]:
    """The positional argument types of the class's ``@workflow.run`` method (after
    ``self``). Resolves string annotations via the method's own globals, so it works
    under ``from __future__ import annotations``. Raises ``TypeError`` if the class has
    no ``@workflow.run`` method to inspect."""
    run_fn = next(
        (
            member
            for _, member in inspect.getmembers(cls, inspect.isfunction)
            if getattr(member, "__temporal_workflow_run", False)
        ),
        None,
    )
    if run_fn is None:
        raise TypeError(
            f"@agent.defn requires {cls.__name__!r} to define a @workflow.run method "
            f"(stack it with @workflow.defn on an agent workflow class)."
        )
    hints = get_type_hints(run_fn)
    positional = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    return [
        hints.get(p.name)
        for p in inspect.signature(run_fn).parameters.values()
        if p.name != "self" and p.kind in positional
    ]


# ---------------------------------------------------------------------------
# @agent.accepts — declare a message handler the runner discovers and dispatches to
# ---------------------------------------------------------------------------
#
# Each accepted message is an ``async def (self, msg: InputModel) -> OutputModel`` method
# marked ``@agent.accepts``. The harness discovers them (validating the contract at import
# time via ``defn``), routes an inbound ``send_agent_message`` to the handler whose name
# matches the envelope ``type``, validates the ``payload`` into the handler's input model,
# and publishes the handler's RETURN value as the turn's reply. No phantom types, no manual
# reply, no ``turns()`` loop — the dev's ``(self, msg) -> Output`` signature is fully typed.

_ACCEPTS_MARKER = "__agent_accepts__"
_HANDLERS_ATTR = "__agent_handlers__"


def accepts(fn: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
    """Mark an agent method as a message handler (returned unchanged).

    The method must be ``async def name(self, msg: InputModel) -> OutputModel`` where both
    ``InputModel`` and ``OutputModel`` are pydantic models with docstrings (the input
    model's fields become the tool ``parameters`` schema; the handler's own docstring is
    its tool ``description``). The function name is the tool name and the value a caller
    sends in :attr:`AgentMessage.type`. Discovery + validation happen at import time in
    :func:`defn`; a malformed handler raises :class:`TypeError` then.
    """
    setattr(fn, _ACCEPTS_MARKER, True)
    return fn


@dataclass(frozen=True)
class _AcceptedHandler:
    """One discovered ``@agent.accepts`` handler — the unit of dispatch + introspection."""

    name: str
    input_type: type[BaseModel]
    output_type: type[BaseModel]
    description: str
    method: Callable[..., Awaitable[Any]]


def _discover_handlers(cls: type) -> dict[str, _AcceptedHandler]:
    """Find + validate every ``@agent.accepts`` handler on ``cls`` (no instance/workflow).

    Pure reflection over the class — the single source for both runtime dispatch and the
    ``agent_interface`` discovery query (and the subagent toolset generator). Raises
    ``TypeError`` on any contract violation so a malformed agent fails at import.
    """
    handlers: dict[str, _AcceptedHandler] = {}
    for name, fn in inspect.getmembers(cls, inspect.isfunction):
        if not getattr(fn, _ACCEPTS_MARKER, False):
            continue
        hints = get_type_hints(fn)
        params = [p for p in inspect.signature(fn).parameters if p != "self"]
        if len(params) != 1:
            raise TypeError(
                f"@agent.accepts {name!r} must take exactly one argument besides self "
                f"(the input message model); got {params}."
            )
        input_type = hints.get(params[0])
        output_type = hints.get("return")
        if not (isinstance(input_type, type) and issubclass(input_type, BaseModel)):
            raise TypeError(
                f"@agent.accepts {name!r}: its argument must be annotated with a pydantic "
                f"model (the input message type); got {input_type!r}."
            )
        if not (isinstance(output_type, type) and issubclass(output_type, BaseModel)):
            raise TypeError(
                f"@agent.accepts {name!r}: its return type must be a pydantic model "
                f"(no scalar/None returns); got {output_type!r}."
            )
        for who in (fn, input_type, output_type):
            if not (getattr(who, "__doc__", None) or "").strip():
                raise TypeError(
                    f"@agent.accepts {name!r}: {who.__name__} must have a docstring "
                    f"(handler docstring → tool description; model docstrings → schema docs)."
                )
        handlers[name] = _AcceptedHandler(
            name=name,
            input_type=input_type,
            output_type=output_type,
            description=(fn.__doc__ or "").strip(),
            method=fn,
        )
    return handlers


def agent_handlers(cls: type) -> dict[str, _AcceptedHandler]:
    """The discovered handler map for an agent class (empty if it has none).

    Read from the attribute ``defn`` stamps at import; falls back to discovering on the fly
    (so a bare ``@workflow.defn`` agent, or an offline call, still works)."""
    discovered = getattr(cls, _HANDLERS_ATTR, None)
    if discovered is None:
        discovered = _discover_handlers(cls)
    return discovered


@overload
def defn(cls: _WorkflowClass, /) -> _WorkflowClass: ...
@overload
def defn(cls: None = None, /) -> Callable[[_WorkflowClass], _WorkflowClass]: ...
def defn(cls: type | None = None, /) -> Any:
    """Validate that a class honors the standardized agent contract, returning it
    unchanged. Stack it WITH ``@workflow.defn`` — it does not replace it (Temporal stays
    visible)::

        @workflow.defn(name="MyAgent")
        @agent.defn
        class MyAgent:
            @workflow.run
            async def run(self, config: AgentConfig) -> None: ...

    The check runs at definition (import) time: it inspects the ``@workflow.run`` method
    and requires its arguments to be exactly one :class:`AgentConfig`. A misconfigured
    agent therefore raises :class:`TypeError` the moment its module is imported (e.g. at
    worker startup), with a clear message — instead of starting and then hanging by
    repeatedly failing its first workflow task at execution time (where the caller would
    only ever see a timeout).

    Order-independent with ``@workflow.defn`` (it keys off the ``@workflow.run`` marker,
    which class-body evaluation sets before either class decorator runs). The same
    contract is re-checked when the runner is built
    (:func:`_assert_standardized_agent_signature`), as a backstop for any agent declared
    with a bare ``@workflow.defn``.
    """

    def decorate(c: type) -> type:
        _validate_agent_arg_types(c.__name__, _workflow_run_arg_types(c))
        # Discover + validate the @agent.accepts handlers now (import time), and stamp them
        # so the runner / agent_interface query / subagent generator read them without
        # re-introspecting (and a malformed handler fails fast at import).
        setattr(c, _HANDLERS_ATTR, _discover_handlers(c))
        return c

    return decorate(cls) if cls is not None else decorate


# ---------------------------------------------------------------------------
# AgentToolContext — the {stream context + tool id} ferried into a tool activity
# ---------------------------------------------------------------------------


class AgentToolContext(BaseModel):
    """The bundle a tool needs to publish its own lifecycle events from inside its
    activity: which turn to publish against (:attr:`stream_context`) and the id that
    correlates this one invocation's events (:attr:`tool_id`).

    A ``ContextVar`` cannot cross the activity process boundary, so for an activity
    tool this value is serialized and passed as the trailing activity argument by the
    :func:`activity_tool_defn` dispatcher; the decorator's activity body reads it back to
    open a :meth:`AgentWorkflowRunner.publisher_from_activity`. (A pure in-workflow tool —
    :func:`tool_defn` — needs no such carrier, as it publishes in-process, so this type is
    only built for the activity path.)
    """

    stream_context: TurnStreamContext
    tool_id: str

    @classmethod
    def for_current_tool_id(cls) -> AgentToolContext:
        """Build the context for the in-flight tool call — both fields resolved implicitly.

        The tool id (the model's per-call id) and the turn (stream context) are both
        read from the ambient state ``run_tool`` parked for this invocation, so the
        caller threads nothing. Raises if there is no current tool call or active turn.
        """
        tool_id = _current_tool_id()
        runner = _CURRENT_RUNNER.get()
        stream_context = runner.current_stream_context if runner is not None else None
        if stream_context is None:
            raise RuntimeError(
                "no active agent turn — AgentToolContext.for_current_tool_id() must "
                "be called while a turn is in flight"
            )
        return cls(stream_context=stream_context, tool_id=tool_id)


# ---------------------------------------------------------------------------
# TurnEventPublisher — minimal activity-side publish interface
# ---------------------------------------------------------------------------


class TurnEventPublisher:
    """Activity-side handle that publishes events to a workflow's stream.

    Bound to a :class:`TurnStreamContext` at construction so call sites
    don't have to re-thread turn metadata on every publish. Obtain one
    via :meth:`AgentWorkflowRunner.publisher_from_activity` (an async
    context manager that owns the underlying
    :class:`WorkflowStreamClient` lifecycle so deltas batch and drain
    correctly).
    """

    def __init__(
        self,
        events: TopicHandle[AgentEvent],
        context: TurnStreamContext,
    ) -> None:
        self._events = events
        self._context = context

    def publish(self, event: AgentStreamItem) -> None:
        """Wrap ``event`` in an :class:`AgentEvent` envelope and publish it.

        Producers build the typed payload (e.g. ``ReplyDelta(text=…)``); the
        envelope adds the routing metadata only the harness controls — ``turn_id``
        / ``turn_number`` / ``agent_id`` (all from the bound :class:`TurnStreamContext`,
        which the workflow side threaded into the activity) and ``timestamp`` (wall-clock;
        the workflow side uses ``workflow.time()`` — both serialize identically).
        """
        self._events.publish(
            AgentEvent(
                event=event,
                agent_id=self._context.agent_id,
                turn_id=self._context.turn_id,
                turn_number=self._context.turn_number,
                timestamp=time.time(),
            )
        )


# ---------------------------------------------------------------------------
# Internal status tracker
# ---------------------------------------------------------------------------


class _ApprovalStatus(StrEnum):
    """Lifecycle of one gated tool call's human-approval decision."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


@dataclass
class _ApprovalEntry:
    """One gated tool call's approval record, keyed by its per-call tool id.

    Created PENDING when the gated tool is invoked; flipped to APPROVED/DENIED by the
    ``tool_approval`` update (or auto-DENIED if the agent closes while still pending).
    Resolved entries are RETAINED (status flips, the entry is not removed) so the update
    validator can tell "unknown id" from "already resolved".
    """

    tool_id: str
    tool_name: str
    tool_input: dict[str, Any]
    turn_number: int
    # The turn this call belongs to — retained so the resolution can be published against
    # the owning turn even from an update handler that isn't itself "in" that turn.
    turn_id: str
    # The tool's static ``inherently_safe`` self-assertion, retained so a runtime policy
    # change can re-evaluate this still-PENDING call against the new policy.
    inherently_safe: bool
    status: _ApprovalStatus = _ApprovalStatus.PENDING
    reason: str | None = None
    # Whether the resolving decision asked to be remembered (allow-list this tool). False
    # for one-off decisions and for calls auto-resolved by a policy change.
    remember: bool = False


@dataclass
class _ApprovalOutcome:
    """The terminal result of an approval wait — what the gate acts on."""

    approved: bool
    reason: str | None = None
    remember: bool = False


class _CallbackStatus(StrEnum):
    """Lifecycle of one callback tool call's external-result fulfillment."""

    PENDING = "pending"
    RESOLVED = "resolved"


@dataclass
class _CallbackEntry:
    """One pending callback tool call, keyed by its per-call tool id.

    A callback tool has no worker-side body: it pauses in-workflow while an attached client
    executes it and returns a result via the ``provide_callback_result`` update. Created PENDING
    when the tool body registers the wait; flipped to RESOLVED by that update (or finalized as a
    timeout / close by the gate itself). Resolved entries are RETAINED (status flips, the entry is
    not removed) so the update validator can tell "unknown id" from "already resolved".

    ``output_adapter`` is a runtime pydantic :class:`TypeAdapter` for the tool's declared output
    type — used to validate the client's payload. It lives only in workflow memory (rebuilt on
    replay when the tool body re-runs) and is never serialized.
    """

    tool_id: str
    tool_name: str
    tool_input: dict[str, Any]
    output_schema: dict[str, Any]
    turn_number: int
    # The turn this call belongs to — retained so the resolution can be published against the
    # owning turn even from an update handler that isn't itself "in" that turn.
    turn_id: str
    output_adapter: TypeAdapter[Any]
    status: _CallbackStatus = _CallbackStatus.PENDING
    outcome: Literal["ok", "error", "timeout"] = "ok"
    # The validated output object on ``ok`` (the tool's return value); None otherwise.
    result: Any = None
    # The client-reported failure ('error'), or the timeout/close note ('timeout'); None on 'ok'.
    error: str | None = None


@dataclass
class _CallbackOutcome:
    """The terminal result of a callback wait — what the gate acts on."""

    outcome: Literal["ok", "error", "timeout"]
    result: Any = None
    error: str | None = None


@dataclass
class _SubagentInstance:
    """One running subagent this agent drives, keyed by its short model-facing ``handle``.

    ``handle`` is a short, tree-unique id the model uses to reference this subagent in
    ``send_<function>`` / ``stop_<key>`` calls (this agent's own id plus one fresh segment — see
    :meth:`AgentWorkflowRunner._fresh_subagent_handle`) — far cheaper for the model to reproduce
    than the full child ``workflow_id`` (which it never sees). The workflow-side code maps
    ``handle`` → ``workflow_id`` and passes the real ``workflow_id`` to the ``run_subagent_turn``
    activity.

    Holds the per-subagent turn bookkeeping the ``send_<function>`` tool threads to the
    ``run_subagent_turn`` activity: ``next_expected_turn`` (the child's next turn number, sent
    as ``expected_turn`` and advanced as turns complete) and ``last_consumed_offset`` (the
    child stream position to resume the next turn from — a perf hint).

    It also owns a **FIFO gate** that serializes this subagent's turns ON THE CALLER SIDE: when
    a parent issues several ``send_<function>`` calls to the same subagent at once (e.g.
    ``asyncio.gather``), each takes a monotonically increasing ticket (synchronously, in call
    order) and waits until it is the one being served. Only one turn runs at a time, so the
    turn number read after the wait is always exact — no prediction, no arrival-order race.
    Gates are per-subagent, so different subagents still run concurrently. (A subagent processes
    turns sequentially regardless, so this serialization costs no throughput.)
    """

    handle: str
    workflow_id: str
    agent_key: str
    next_expected_turn: int = 1
    last_consumed_offset: int = 0
    # FIFO gate: tickets handed out in call order; the holder whose ticket == _serving runs.
    _next_ticket: int = 0
    _serving: int = 0

    def take_ticket(self) -> int:
        """Reserve the next gate position. MUST be called synchronously (before any await) so
        gathered callers are ordered by the model's call order, not by await scheduling."""
        ticket = self._next_ticket
        self._next_ticket += 1
        return ticket

    def is_serving(self, ticket: int) -> bool:
        """Whether ``ticket`` is the one currently allowed through the gate."""
        return self._serving == ticket

    def release_gate(self) -> None:
        """Pass the gate to the next ticket. Called by the holder when its turn finishes."""
        self._serving += 1


class _WorkflowStatus:
    """Internal workflow state that owns all status fields and the pending queue.

    All fields are private. Mutations go through methods so state
    transitions stay consistent (e.g. starting a turn always sets
    turn_active and increments current_turn atomically).

    ``is_message_queuing_enabled`` is an agent-level capability set at
    construction and immutable for the lifetime of the workflow.

    ``approval_policy`` is the live :class:`ToolApprovalPolicy`. Unlike message queuing it
    is *mutable* — :meth:`set_approval_policy` swaps it for a runtime policy update — and
    is surfaced on the ``agent_status`` query. ``has_custom_approval_fallback`` records
    only whether a developer fallback predicate is wired (for the status query); the
    predicate itself lives on the runner, never here.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        is_message_queuing_enabled: bool,
        approval_policy: ToolApprovalPolicy,
        has_custom_approval_fallback: bool = False,
    ) -> None:
        # This agent's own short id — stamped on every event (via current_stream_context for
        # activity publishes, and _pub for in-workflow ones) and surfaced on the status query.
        self._agent_id: str = agent_id
        self._current_turn: int = 0
        self._current_turn_id: str | None = None
        self._turn_active: bool = False
        self._pending_turns: list[tuple[AgentMessage, str]] = []
        self._is_message_queuing_enabled: bool = is_message_queuing_enabled
        self._approval_policy: ToolApprovalPolicy = approval_policy
        self._has_custom_approval_fallback: bool = has_custom_approval_fallback
        # Gated tool calls awaiting a human decision, keyed by per-call tool id.
        # Entries are retained after resolution (status flips) for idempotency.
        self._approvals: dict[str, _ApprovalEntry] = {}
        # Callback tool calls awaiting a client-supplied result, keyed by per-call tool id.
        # Entries are retained after resolution (status flips) for idempotency.
        self._callbacks: dict[str, _CallbackEntry] = {}
        # Subagents this agent is driving, keyed by child ``subagent_id``.
        self._subagents: dict[str, _SubagentInstance] = {}

    @property
    def current_turn(self) -> int:
        return self._current_turn

    @property
    def current_stream_context(self) -> TurnStreamContext | None:
        """Stream-publishing carrier for the in-flight turn, or ``None`` if idle.

        Distinct from :attr:`current_turn`, which is the monotonic counter
        and stays pinned at the last turn's number even after that turn
        completes.
        """
        if self._current_turn_id is None:
            return None
        return TurnStreamContext(
            turn_id=self._current_turn_id,
            turn_number=self._current_turn,
            agent_id=self._agent_id,
        )

    @property
    def is_message_queuing_enabled(self) -> bool:
        return self._is_message_queuing_enabled

    @property
    def has_pending_work(self) -> bool:
        return self._turn_active or len(self._pending_turns) > 0

    @property
    def has_pending_turns(self) -> bool:
        return len(self._pending_turns) > 0

    @property
    def next_turn_number(self) -> int:
        return self._current_turn + len(self._pending_turns) + 1

    def enqueue_message(self, message: AgentMessage, turn_id: str) -> int:
        """Add a message to the pending queue. Returns its assigned turn number."""
        self._pending_turns.append((message, turn_id))
        return self._current_turn + len(self._pending_turns)

    def start_next_turn(self) -> tuple[AgentMessage, str]:
        """Pop the next pending message and begin the turn."""
        message, turn_id = self._pending_turns.pop(0)
        self._current_turn += 1
        self._current_turn_id = turn_id
        self._turn_active = True
        return message, turn_id

    def complete_turn(self) -> None:
        """Mark the current turn as finished."""
        self._turn_active = False
        self._current_turn_id = None

    # -- Tool-approval registry ---------------------------------------------

    def register_pending_approval(
        self,
        tool_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        turn_number: int,
        turn_id: str,
        *,
        inherently_safe: bool,
    ) -> None:
        """Record a gated tool call as PENDING approval. Called by the gate when the
        active policy does not auto-approve the call."""
        self._approvals[tool_id] = _ApprovalEntry(
            tool_id=tool_id,
            tool_name=tool_name,
            tool_input=tool_input,
            turn_number=turn_number,
            turn_id=turn_id,
            inherently_safe=inherently_safe,
        )

    def approval_entry(self, tool_id: str) -> _ApprovalEntry | None:
        """The approval record for ``tool_id`` (any status), or ``None`` if unknown."""
        return self._approvals.get(tool_id)

    def is_approval_resolved(self, tool_id: str) -> bool:
        """True once ``tool_id``'s approval is no longer PENDING. The gate's wait
        condition reads this; an unknown id counts as unresolved."""
        entry = self._approvals.get(tool_id)
        return entry is not None and entry.status is not _ApprovalStatus.PENDING

    def resolve_approval(
        self, tool_id: str, *, approved: bool, reason: str | None, remember: bool = False
    ) -> None:
        """Apply a human decision to a PENDING approval. The update validator has
        already rejected unknown / already-resolved ids, so this just flips status.
        ``remember`` records whether the decision asked to allow-list the tool."""
        entry = self._approvals[tool_id]
        entry.status = _ApprovalStatus.APPROVED if approved else _ApprovalStatus.DENIED
        entry.reason = reason
        entry.remember = remember

    def finalize_approval(self, tool_id: str, *, closed: bool) -> _ApprovalOutcome:
        """Resolve the gate's wait into an outcome. If the entry is still PENDING the
        wait must have woken on agent close — auto-deny it so the workflow winds down."""
        entry = self._approvals[tool_id]
        if entry.status is _ApprovalStatus.PENDING:
            # Woke because the agent is closing, not because of a decision.
            reason = "agent closed before approval" if closed else "approval unresolved"
            entry.status = _ApprovalStatus.DENIED
            entry.reason = reason
        return _ApprovalOutcome(
            approved=entry.status is _ApprovalStatus.APPROVED,
            reason=entry.reason,
            remember=entry.remember,
        )

    def pending_approvals(self) -> list[PendingApproval]:
        """All gated tool calls still awaiting a decision (for the status query)."""
        return [
            PendingApproval(
                tool_id=e.tool_id,
                tool_name=e.tool_name,
                tool_input=e.tool_input,
                turn_number=e.turn_number,
            )
            for e in self._approvals.values()
            if e.status is _ApprovalStatus.PENDING
        ]

    def pending_approval_entries(self) -> list[_ApprovalEntry]:
        """The live PENDING approval entries (full records, including ``inherently_safe``).

        Used by a runtime policy update to re-evaluate each still-waiting call against the
        new policy — distinct from :meth:`pending_approvals`, which projects the trimmed,
        client-facing :class:`PendingApproval` view."""
        return [
            e for e in self._approvals.values() if e.status is _ApprovalStatus.PENDING
        ]

    @property
    def approval_policy(self) -> ToolApprovalPolicy:
        """The live tool-approval policy (swapped by :meth:`set_approval_policy`)."""
        return self._approval_policy

    def set_approval_policy(self, policy: ToolApprovalPolicy) -> None:
        """Replace the live approval policy. The runner re-evaluates pending approvals
        against it — see :meth:`AgentWorkflowRunner._apply_policy_update`."""
        self._approval_policy = policy

    # -- Callback-tool registry ---------------------------------------------

    def register_pending_callback(
        self,
        tool_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        output_schema: dict[str, Any],
        turn_number: int,
        turn_id: str,
        output_adapter: TypeAdapter[Any],
    ) -> None:
        """Record a callback tool call as PENDING a client-supplied result. Called by the
        callback tool's body when it parks to await external fulfillment."""
        self._callbacks[tool_id] = _CallbackEntry(
            tool_id=tool_id,
            tool_name=tool_name,
            tool_input=tool_input,
            output_schema=output_schema,
            turn_number=turn_number,
            turn_id=turn_id,
            output_adapter=output_adapter,
        )

    def callback_entry(self, tool_id: str) -> _CallbackEntry | None:
        """The callback record for ``tool_id`` (any status), or ``None`` if unknown."""
        return self._callbacks.get(tool_id)

    def is_callback_resolved(self, tool_id: str) -> bool:
        """True once ``tool_id``'s callback is no longer PENDING. The gate's wait condition
        reads this; an unknown id counts as unresolved."""
        entry = self._callbacks.get(tool_id)
        return entry is not None and entry.status is not _CallbackStatus.PENDING

    def resolve_callback(
        self,
        tool_id: str,
        *,
        outcome: Literal["ok", "error", "timeout"],
        result: Any,
        error: str | None,
    ) -> None:
        """Apply an external result to a PENDING callback. The update validator has already
        rejected unknown / already-resolved ids and (on ``ok``) checked the payload against
        the output type, so this just flips status and records the outcome."""
        entry = self._callbacks[tool_id]
        entry.status = _CallbackStatus.RESOLVED
        entry.outcome = outcome
        entry.result = result
        entry.error = error

    def finalize_callback(
        self, tool_id: str, *, closed: bool, timed_out: bool = False
    ) -> _CallbackOutcome:
        """Resolve the gate's wait into an outcome. If the entry is still PENDING the wait must
        have woken on timeout or agent close — record it as a failure so the tool can raise."""
        entry = self._callbacks[tool_id]
        if entry.status is _CallbackStatus.PENDING:
            if timed_out:
                reason = "callback timed out before a result arrived"
            elif closed:
                reason = "agent closed before the callback result arrived"
            else:
                reason = "callback unresolved"
            entry.status = _CallbackStatus.RESOLVED
            entry.outcome = "timeout"
            entry.error = reason
        return _CallbackOutcome(
            outcome=entry.outcome, result=entry.result, error=entry.error
        )

    def pending_callbacks(self) -> list[PendingCallback]:
        """All callback tool calls still awaiting a client result (for the status query)."""
        return [
            PendingCallback(
                tool_id=e.tool_id,
                tool_name=e.tool_name,
                tool_input=e.tool_input,
                output_schema=e.output_schema,
                turn_number=e.turn_number,
            )
            for e in self._callbacks.values()
            if e.status is _CallbackStatus.PENDING
        ]

    # -- Subagent registry --------------------------------------------------

    def has_subagent(self, handle: str) -> bool:
        """Whether ``handle`` is already in use — used to keep generated handles unique."""
        return handle in self._subagents

    def register_subagent(
        self, handle: str, workflow_id: str, agent_key: str
    ) -> _SubagentInstance:
        """Record a freshly-started subagent under its short ``handle`` and return its entry."""
        inst = _SubagentInstance(
            handle=handle, workflow_id=workflow_id, agent_key=agent_key
        )
        self._subagents[handle] = inst
        return inst

    def subagent(self, handle: str) -> _SubagentInstance:
        """The tracking entry for ``handle``, or raise if the parent never started it.

        Guards the ``send_<function>`` / ``stop_<key>`` tools against a model-supplied handle
        that doesn't name a subagent this agent is driving (its own, or one already stopped)."""
        inst = self._subagents.get(handle)
        if inst is None:
            raise ApplicationError(
                f"Unknown subagent {handle!r}. Known subagents: "
                f"{sorted(self._subagents)}.",
                {"handle": handle, "known": sorted(self._subagents)},
                type="UnknownSubagent",
                non_retryable=True,
            )
        return inst

    def remove_subagent(self, handle: str) -> None:
        """Drop a subagent from the registry (after it is stopped). Idempotent."""
        self._subagents.pop(handle, None)

    def active_subagents(self) -> list[SubagentInfo]:
        """The active subagents projected for the ``agent_status`` query.

        Surfaces the subagent_id / agent_key / real ``workflow_id`` / next turn — deliberately NOT
        the gate's ticket counters (an internal turn-ordering detail, not status)."""
        return [
            SubagentInfo(
                subagent_id=inst.handle,
                agent_key=inst.agent_key,
                workflow_id=inst.workflow_id,
                next_expected_turn=inst.next_expected_turn,
            )
            for inst in self._subagents.values()
        ]

    def to_agent_status(self) -> AgentStatus:
        return AgentStatus(
            agent_id=self._agent_id,
            current_turn=self._current_turn,
            turn_active=self._turn_active,
            pending_turns=[
                PendingTurn(
                    turn_number=self._current_turn + i + 1,
                    turn_id=turn_id,
                    message=_render_message(message),
                )
                for i, (message, turn_id) in enumerate(self._pending_turns)
            ],
            is_message_queuing_enabled=self._is_message_queuing_enabled,
            pending_approvals=self.pending_approvals(),
            pending_callbacks=self.pending_callbacks(),
            subagents=self.active_subagents(),
            approval_policy=self._approval_policy,
            has_custom_approval_fallback=self._has_custom_approval_fallback,
        )


# ---------------------------------------------------------------------------
# AgentWorkflowRunner
# ---------------------------------------------------------------------------

def current_agent_workflow_runner() -> "AgentWorkflowRunner | None":
    """Return the calling workflow's :class:`AgentWorkflowRunner`, or ``None`` outside a
    workflow, before ``@workflow.init`` runs, or in a workflow that isn't a harness agent.

    Same lookup :func:`current_stream_context` already uses (harness agents store their
    runner as ``self._runner`` on the workflow instance) — reused here rather than adding a
    second, redundant stashing mechanism.
    """
    if not workflow.in_workflow():
        return None
    runner = getattr(workflow.instance(), "_runner", None)
    return runner if isinstance(runner, AgentWorkflowRunner) else None


class AgentWorkflowRunner:
    """Workflow-side agent runtime: discovers ``@agent.accepts`` handlers and dispatches.

    Construct it directly inside ``@workflow.init`` with the agent's :class:`AgentConfig`
    plus the agent's defaults (``stream`` and ``approval_policy_default`` are required); see
    :meth:`__init__`.

    Registers the update, query, and signal handlers the ``AgentClient`` protocol requires;
    routes each inbound ``send_agent_message`` envelope to the handler named by its ``type``
    (rejecting an unknown function or a malformed payload at the update boundary); answers
    the ``agent_interface`` discovery query (the handlers described tool-style); and drives
    the turn loop via :meth:`run`, publishing each handler's return value as the reply.
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        stream: WorkflowStream,
        approval_policy_default: ToolApprovalPolicy,
        enable_message_queuing_default: bool = False,
        custom_approval_fallback: CustomApprovalFallback | None = None,
        slash_commands: Iterable[SlashCommandDefinition] | None = None,
        enabled_mcp_servers_default: Iterable[str] | None = None,
    ) -> None:
        """Construct the runner inside the agent's ``@workflow.init``::

            self._runner = AgentWorkflowRunner(
                config,
                stream=WorkflowStream(),
                approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
            )

        ``config`` is the standardized agent input; the runner resolves each universal
        knob as *the caller's config value if given, else the agent's default*
        (``approval_policy_default`` is required — the author must make a deliberate
        safe-by-default choice; ``enable_message_queuing_default`` defaults to the harness
        baseline of disabled). The accepted messages are NOT configured here — they are
        discovered from the agent's ``@agent.accepts`` handler methods. Registers the
        workflow's update/query/signal handlers. ``slash_commands`` configures the
        human/operator slash command registry used by both first-class operator updates
        and normal ``slash`` turns. If omitted, the packaged harness defaults are enabled;
        pass an empty iterable to disable packaged slash commands. ``enabled_mcp_servers_default``
        is the agent's own opt-in default (harness baseline: none — MCP tools are opt-in, never
        available just because they're technically reachable); see
        :attr:`AgentConfig.enabled_mcp_servers` and :meth:`set_enabled_mcp_servers`.
        """
        # The runner is built inside the agent's @workflow.init; enforce here that the
        # enclosing workflow honors the standardized agent-input contract (run/__init__
        # takes a single AgentConfig) so it stays substitutable as a parent or sub-agent.
        _assert_standardized_agent_signature()
        # Discover the @agent.accepts handlers off the enclosing agent class (stamped by
        # @agent.defn at import). Outside a workflow (offline unit tests) there is no
        # enclosing agent class, so there are simply no handlers.
        cls = _enclosing_workflow_class()
        self._handlers: dict[str, _AcceptedHandler] = (
            agent_handlers(cls) if cls is not None else {}
        )
        self._slash_commands: tuple[SlashCommandDefinition, ...] = tuple(
            slash_commands if slash_commands is not None else default_commands()
        )
        # Resolve each knob: the caller's config value wins when given; otherwise fall back
        # to the agent's default. The caller can never be overridden — the agent only fills
        # gaps.
        approval_policy = (
            config.approval_policy
            if config.approval_policy is not None
            else approval_policy_default
        )
        is_message_queuing_enabled = (
            config.is_message_queuing_enabled
            if config.is_message_queuing_enabled is not None
            else enable_message_queuing_default
        )
        # Opt-in only: an unset config AND an unset agent default both mean "nothing enabled",
        # never "everything reachable" — see AgentConfig.enabled_mcp_servers.
        self._enabled_mcp_servers: frozenset[str] = frozenset(
            config.enabled_mcp_servers
            if config.enabled_mcp_servers is not None
            else (enabled_mcp_servers_default or [])
        )
        # This agent's short id, stamped on every event it publishes and reported on its status
        # query. A parent assigns it when starting a subagent (pushing down the same short handle
        # it references the child by — see start_subagent); a top-level agent left with no id
        # generates its own. workflow.uuid4 is deterministic in-workflow (offline unit tests patch
        # it). Distinct from the full workflow_id, which the model/UI never needs to reproduce.
        self._agent_id: str = config.agent_id or workflow.uuid4().hex[:AGENT_ID_LENGTH]
        # Retain the WorkflowStream itself (not just the topic handle) so the runner can read
        # the stream's current head offset in-workflow — see ``_handle_send_agent_message``,
        # which returns it as ``AgentMessageReply.accepted_offset`` for the client stream-merge.
        self._stream = stream
        self._events: WorkflowTopicHandle[AgentEvent] = stream.topic(
            TURN_EVENTS_TOPIC, type=AgentEvent
        )
        self._custom_approval_fallback = custom_approval_fallback
        self._status = _WorkflowStatus(
            agent_id=self._agent_id,
            is_message_queuing_enabled=is_message_queuing_enabled,
            approval_policy=approval_policy,
            has_custom_approval_fallback=custom_approval_fallback is not None,
        )
        self._closed = False

        # Register protocol handlers dynamically so the containing workflow doesn't need to.
        workflow.set_update_handler(
            SEND_AGENT_MESSAGE_UPDATE,
            self._handle_send_agent_message,
            validator=self._validate_send_agent_message,
        )
        # ``tool_approval`` is a SEPARATE update on purpose — do NOT merge it into
        # ``send_agent_message``. They serve opposite roles and that separation is
        # load-bearing:
        #
        #   * ``send_agent_message`` is the agent's FRONT DOOR. It routes a message into one
        #     of the agent author's ``@agent.accepts`` handlers, and the callable surface is
        #     advertised via the ``agent_interface`` discovery query so any caller —
        #     including a *parent (non-human) agent* — can drive this agent through it.
        #   * ``tool_approval`` is handled ENTIRELY by this harness — the agent author never
        #     sees it; ``_apply_approval_policy`` resolves the gate in-process. It is a
        #     human-in-the-loop guardrail keeping the model from acting unilaterally on a
        #     gated tool.
        #
        # It is therefore DELIBERATELY NOT in ``agent_interface``: an automating parent agent
        # that discovers and speaks this agent's contract must not be able to rubber-stamp
        # its child's tool approvals — that would defeat the guardrail. Approvals come from a
        # human/operator surface out-of-band (the UI's approve/deny), not the agent-to-agent
        # front door. See ``_handle_tool_approval`` and ``_handle_agent_interface``.
        workflow.set_update_handler(
            TOOL_APPROVAL_UPDATE,
            self._handle_tool_approval,
            validator=self._validate_tool_approval,
        )
        # ``provide_callback_result`` is a SEPARATE update from ``tool_approval`` on purpose,
        # even though both resolve a pending gate keyed by ``tool_id``. A callback tool call
        # passes through BOTH gates in sequence on the same id — first the approval gate in the
        # dispatch prologue, then the callback gate in the tool body — so one shared "resolve"
        # handler would be ambiguous about which gate a submission targets. Two focused handlers
        # (each with its own typed payload and registry) keep the two sequential gates clean.
        # Like ``tool_approval``, this is harness-owned and NOT advertised via ``agent_interface``
        # (see ``_handle_agent_interface``): a callback result comes from the attached client
        # fulfilling the tool on its own machine, an out-of-band control-plane surface — not the
        # agent-to-agent front door a parent agent drives.
        workflow.set_update_handler(
            PROVIDE_CALLBACK_RESULT_UPDATE,
            self._handle_provide_callback_result,
            validator=self._validate_provide_callback_result,
        )
        workflow.set_update_handler(
            EXECUTE_OPERATOR_COMMAND_UPDATE,
            self._handle_execute_operator_command,
        )
        # Lets a caller turn MCP integrations on/off mid-conversation without starting a new
        # session — see AgentConfig.enabled_mcp_servers for why this is opt-in, not opt-out.
        # Discoverable via current_agent_workflow_runner() -- no separate stashing needed,
        # since self._runner (set by the containing workflow's own __init__, per the
        # existing convention current_stream_context() already relies on) is what that
        # lookup reads.
        workflow.set_update_handler(
            SET_ENABLED_MCP_SERVERS_UPDATE, self._handle_set_enabled_mcp_servers
        )
        workflow.set_query_handler(AGENT_STATUS_QUERY, self._handle_agent_status)
        workflow.set_query_handler(AGENT_INTERFACE_QUERY, self._handle_agent_interface)
        workflow.set_query_handler(
            OPERATOR_INTERFACE_QUERY, self._handle_operator_interface
        )
        workflow.set_signal_handler("close", self._handle_close)

    # -- Protocol handlers --------------------------------------------------

    async def _handle_send_agent_message(self, message: AgentMessage) -> AgentMessageReply:
        # Capture the stream head BEFORE publishing anything for this message: it is the
        # client stream-merge's read-start hint (``accepted_offset``). The handler body is
        # synchronous (no await that yields), so this runs atomically before the turn loop can
        # publish this turn's ``turn_started`` — guaranteeing ``accepted_offset <= turn_started``,
        # which is all the merge requires (it discards events up to ``turn_started``). Read the
        # real log head (``_on_offset``), not a publish counter: activity-published events enter
        # the same global log via signals and would be missed by an in-workflow counter.
        accepted_offset = self._stream._on_offset()
        turn_id = str(workflow.uuid4())
        pending = self._status.has_pending_work
        turn_number = self._status.enqueue_message(message, turn_id)

        if pending:
            self._pub(
                turn_id,
                turn_number,
                MessageQueued(user_message=_render_message(message)),
            )

        return AgentMessageReply(
            turn_number=turn_number,
            turn_id=turn_id,
            accepted_offset=accepted_offset,
            pending=pending,
        )

    def _validate_send_agent_message(self, message: AgentMessage) -> None:
        next_turn = self._status.next_turn_number
        if message.expected_turn != next_turn:
            raise ApplicationError(
                f"Stale: expected turn {message.expected_turn} "
                f"but next turn is {next_turn}",
                {"expected_turn": message.expected_turn, "next_turn": next_turn},
                type="StaleTurn",
                non_retryable=True,
            )
        if (
            self._status.has_pending_work
            and not self._status.is_message_queuing_enabled
        ):
            raise ApplicationError(
                "Agent is busy and message queuing is currently disabled.",
                {"current_turn": self._status.current_turn},
                type="AgentBusy",
                non_retryable=True,
            )
        # ``slash`` is a harness-reserved operator command channel. It is accepted for every
        # runner, whether or not the agent has an agent-specific slash extension handler.
        if message.type == _SLASH_MESSAGE_TYPE:
            try:
                SlashCommand.model_validate(message.payload)
            except ValidationError as e:
                raise ApplicationError(
                    f"Payload for function {_SLASH_MESSAGE_TYPE!r} does not match its "
                    f"input model {SlashCommand.__name__}. Validation error: {e}",
                    {"function": _SLASH_MESSAGE_TYPE, "error": str(e)},
                    type="MalformedMessage",
                    non_retryable=True,
                )
            return

        # Route by the envelope ``type`` (the handler's function name); reject an unknown
        # function, then validate the ``payload`` against that handler's input model. So the
        # dispatch loop only ever sees a known handler + an already-coerced input.
        handler = self._handlers.get(message.type)
        if handler is None:
            raise ApplicationError(
                f"Unknown function {message.type!r}. "
                f"Known functions: {sorted(self._handlers)}.",
                {"name": message.type, "known": sorted(self._handlers)},
                type="UnknownFunction",
                non_retryable=True,
            )
        try:
            handler.input_type.model_validate(message.payload)
        except ValidationError as e:
            raise ApplicationError(
                f"Payload for function {message.type!r} does not match its input "
                f"model {handler.input_type.__name__}. Validation error: {e}",
                {"function": message.type, "error": str(e)},
                type="MalformedMessage",
                non_retryable=True,
            )

    def _validate_tool_approval(self, decision: ToolApprovalDecision) -> None:
        """Reject an approval for an unknown tool id, or one already resolved.

        The latter is the idempotency guard: once a human has approved or denied a
        gated call, a second ``tool_approval`` update for the same id fails rather than
        flipping the settled decision. Runs before :meth:`_handle_tool_approval`."""
        entry = self._status.approval_entry(decision.tool_id)
        if entry is None:
            raise ApplicationError(
                f"no pending tool approval for tool_id={decision.tool_id!r}",
                type="UnknownToolApproval",
                non_retryable=True,
            )
        if entry.status is not _ApprovalStatus.PENDING:
            raise ApplicationError(
                f"tool approval for tool_id={decision.tool_id!r} is already "
                f"{entry.status.value}",
                type="ToolApprovalAlreadyResolved",
                non_retryable=True,
            )

    async def _handle_tool_approval(
        self, decision: ToolApprovalDecision
    ) -> ToolApprovalResult:
        """Record a human approval decision; the gate's wait condition observes it on
        the next workflow task and unblocks (approved → dispatch, denied → error).

        DESIGN INVARIANT — keep this distinct from :meth:`_handle_send_agent_message`, and do
        not advertise it via :meth:`_handle_agent_interface`. ``send_agent_message`` is the
        front door that routes a message to the agent author's handler and is part of the
        discovered interface; this handler is internal harness machinery the author
        never sees (the gate resolves in-process). The separation is the human-in-the-loop
        guardrail itself: a parent (non-human) agent drives a child only through the
        front-door ``send_agent_message`` it discovers via ``agent_interface`` — so by
        excluding approvals from that surface, an automating parent cannot approve its
        child's gated tool calls. Approvals are intended to flow from a human/operator
        out-of-band, never from the agent-to-agent channel; merging or publicizing this
        would let the AI rubber-stamp its own dangerous calls, defeating the point.

        ``remember`` ("approve, and stop asking me about this tool"): an approved decision
        carrying it adds the tool to the live policy's allow-list, which also auto-resolves
        any *other* still-pending call of the same tool (via :meth:`_apply_policy_update`).
        It is a no-op on denial (there is no deny-list yet).

        This decision is resolved AND its event published here, before the cascade — so its
        ``tool_approval_resolved`` is causally ordered before the events of any call the
        cascade auto-resolves as a consequence (see :meth:`_resolve_and_publish`)."""
        entry = self._status.approval_entry(decision.tool_id)
        self._resolve_and_publish(
            decision.tool_id,
            approved=decision.approved,
            reason=decision.reason,
            remember=decision.remember,
        )
        if decision.approved and decision.remember and entry is not None:
            self._apply_policy_update(
                self._status.approval_policy.with_tool_allowed(entry.tool_name)
            )
        return ToolApprovalResult(tool_id=decision.tool_id, accepted=True)

    def _handle_execute_operator_command(
        self, request: OperatorCommandRequest
    ) -> OperatorCommandResult:
        """Execute a human/operator command without creating an agent turn.

        This is the first-class execution counterpart to ``operator_interface``. It does
        not enqueue ``send_agent_message``, increment the turn counter, or publish a model
        reply. Configured slash commands mutate runner state directly through a small
        workflow-safe command context.
        """
        command = SlashCommand(name=request.name, arg=request.arg)
        operator_command_id = str(workflow.uuid4())
        command_label = self._operator_command_label(command.name)
        self._pub(
            operator_command_id,
            0,
            OperatorCommandStarted(
                operator_command_id=operator_command_id,
                command_name=command.name,
                command_label=command_label,
                arg=command.arg,
            ),
        )
        try:
            reply = self._handle_slash_command(command)
        except Exception as e:  # noqa: BLE001 — make operator failures durable
            message = str(e) or type(e).__name__
            self._pub(
                operator_command_id,
                0,
                OperatorCommandFailed(
                    operator_command_id=operator_command_id,
                    command_name=command.name,
                    command_label=command_label,
                    arg=command.arg,
                    message=message,
                ),
            )
            return OperatorCommandResult(text=f"Operator command failed: {message}")
        if reply is None:
            text = f"Unknown operator command: `{command.name}`."
            self._pub(
                operator_command_id,
                0,
                OperatorCommandFailed(
                    operator_command_id=operator_command_id,
                    command_name=command.name,
                    command_label=command_label,
                    arg=command.arg,
                    message=text,
                ),
            )
            return OperatorCommandResult(text=text)
        self._pub(
            operator_command_id,
            0,
            OperatorCommandCompleted(
                operator_command_id=operator_command_id,
                command_name=command.name,
                command_label=command_label,
                arg=command.arg,
                text=reply.text,
            ),
        )
        return OperatorCommandResult(text=reply.text)

    # -- Tool-approval policy ----------------------------------------------

    @property
    def current_approval_policy(self) -> ToolApprovalPolicy:
        """The live :class:`ToolApprovalPolicy` the agent is running under."""
        return self._status.approval_policy

    @property
    def current_status(self) -> AgentStatus:
        """A current :class:`AgentStatus` snapshot for in-workflow handlers."""
        return self._status.to_agent_status()

    # -- MCP server opt-in ---------------------------------------------------

    @property
    def enabled_mcp_servers(self) -> frozenset[str]:
        """The MCP service names this session currently opts into.

        Read live by framework-specific glue (e.g. the OpenAI Agents SDK integration's
        Nexus-transport MCP server) via :func:`current_agent_workflow_runner` — a service
        name absent here is neither listed nor callable, regardless of whether it's
        technically reachable. See :attr:`AgentConfig.enabled_mcp_servers`.
        """
        return self._enabled_mcp_servers

    def set_enabled_mcp_servers(self, names: Iterable[str]) -> None:
        """Replace the set of enabled MCP service names at runtime.

        The public, self-serve entry point for a caller to turn integrations on or off
        mid-conversation — registered as the ``set_enabled_mcp_servers`` update, so any
        client with this workflow's handle can call it directly, no new session needed.
        Replaces the whole set (not incremental add/remove); a caller wanting to add or
        remove one name reads :attr:`enabled_mcp_servers` first and computes the new set
        itself.
        """
        self._enabled_mcp_servers = frozenset(names)

    def _handle_set_enabled_mcp_servers(self, names: list[str]) -> None:
        self.set_enabled_mcp_servers(names)

    def set_approval_policy(self, policy: ToolApprovalPolicy) -> None:
        """Swap the agent's tool-approval policy at runtime.

        The public entry point for an agent author to relax (or tighten) approvals on the
        fly — e.g. from a custom accepted-message handler that lets the user say "trust
        this tool from now on". Relaxing the policy auto-resolves any pending call the new
        policy now allows (see :meth:`_apply_policy_update`); the updated policy is
        reflected on the ``agent_status`` query so a client can read and persist it. The
        developer's custom fallback predicate is separate and is not affected.
        """
        self._apply_policy_update(policy)

    def _auto_approves(
        self, tool_name: str, tool_input: dict[str, Any], *, inherently_safe: bool
    ) -> bool:
        """Whether the current policy (or, as a final fallback, the developer's custom
        predicate) auto-approves this call — i.e. it dispatches without a human gate.

        The serializable :class:`ToolApprovalPolicy` layers are checked first; only if
        none approve is the custom fallback consulted (it is the last layer, by design)."""
        if self._status.approval_policy.auto_approves(
            tool_name, inherently_safe=inherently_safe
        ):
            return True
        if self._custom_approval_fallback is not None:
            return self._custom_approval_fallback(
                ToolApprovalContext(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    inherently_safe=inherently_safe,
                )
            )
        return False

    def _apply_policy_update(self, new_policy: ToolApprovalPolicy) -> None:
        """Install ``new_policy`` and release any pending call it now auto-approves.

        Swaps the live policy, then re-evaluates every still-PENDING approval against it
        (and the custom fallback): each one now auto-approved is resolved AND its
        :class:`ToolApprovalResolved` published here, in iteration order. Publishing at the
        resolution site (rather than letting each parked gate publish on wake) is what keeps
        causal order: gates wake together in registration order, which need not match the
        order resolutions happened — so when this update is itself the consequence of an
        explicit decision (an "approve & remember"), that decision's event has already been
        published before any of these. A more *restrictive* update simply leaves pending
        calls pending (they still need an explicit decision)."""
        self._status.set_approval_policy(new_policy)
        for entry in self._status.pending_approval_entries():
            if self._auto_approves(
                entry.tool_name, entry.tool_input, inherently_safe=entry.inherently_safe
            ):
                self._resolve_and_publish(
                    entry.tool_id,
                    approved=True,
                    reason="auto-approved by updated policy",
                )

    def _resolve_and_publish(
        self,
        tool_id: str,
        *,
        approved: bool,
        reason: str | None,
        remember: bool = False,
    ) -> None:
        """Flip a pending approval to its decision and publish its resolution event NOW.

        The single resolution site for explicit decisions and policy-update cascades, so
        :class:`ToolApprovalResolved` events are emitted in the synchronous order the
        decisions are made — preserving causal order across calls that resolve one another
        (see :meth:`_apply_policy_update`). The close-while-pending auto-deny is the only
        resolution NOT routed through here; the gate publishes that one itself."""
        self._status.resolve_approval(
            tool_id, approved=approved, reason=reason, remember=remember
        )
        self._publish_approval_resolved(tool_id)

    def _publish_approval_resolved(self, tool_id: str) -> None:
        """Publish the :class:`ToolApprovalResolved` for an already-resolved entry, against
        the turn that owns the call (not the workflow's notion of the "current" turn — an
        update handler driving a policy cascade isn't bound to any one turn)."""
        entry = self._status.approval_entry(tool_id)
        if entry is None:
            return
        self._pub(
            entry.turn_id,
            entry.turn_number,
            ToolApprovalResolved(
                tool_id=entry.tool_id,
                tool_name=entry.tool_name,
                approved=entry.status is _ApprovalStatus.APPROVED,
                reason=entry.reason,
                remember=entry.remember,
            ),
        )

    # -- Callback tools -----------------------------------------------------

    def _validate_provide_callback_result(self, result: CallbackResult) -> None:
        """Reject a callback result for an unknown tool id, one already resolved, or (when a
        result rather than an error is supplied) a payload that fails the tool's output type.

        The unknown/already-resolved checks are the idempotency guard (a double-submit fails
        rather than overwriting a settled result). The type check runs HERE, at the update
        boundary, so a malformed payload is rejected synchronously WITHOUT consuming the
        one-shot gate — the client learns immediately and can resubmit a corrected result.
        Runs before :meth:`_handle_provide_callback_result`."""
        entry = self._status.callback_entry(result.tool_id)
        if entry is None:
            raise ApplicationError(
                f"no pending callback for tool_id={result.tool_id!r}",
                type="UnknownCallback",
                non_retryable=True,
            )
        if entry.status is not _CallbackStatus.PENDING:
            raise ApplicationError(
                f"callback for tool_id={result.tool_id!r} is already "
                f"{entry.status.value}",
                type="CallbackAlreadyResolved",
                non_retryable=True,
            )
        # A client-reported error is a valid resolution regardless of the output type; only a
        # positive result must match the declared output.
        if result.error is None:
            try:
                entry.output_adapter.validate_python(result.result)
            except ValidationError as e:
                raise ApplicationError(
                    f"callback result for tool_id={result.tool_id!r} does not match the "
                    f"tool's declared output type. Validation error: {e}",
                    {"tool_id": result.tool_id, "error": str(e)},
                    type="MalformedCallbackResult",
                    non_retryable=True,
                )

    async def _handle_provide_callback_result(
        self, result: CallbackResult
    ) -> CallbackResultAck:
        """Record an external client's result for a pending callback tool call; the tool's
        in-workflow wait observes it on the next workflow task and unblocks (result → return
        the validated value, error → raise to the model).

        DESIGN INVARIANT — like :meth:`_handle_tool_approval`, this is internal harness
        machinery kept OFF the ``agent_interface`` surface. A callback result is supplied by
        the attached client that opted into implementing the tool on its own machine, an
        out-of-band control-plane action — never by the agent-to-agent front door. Excluding it
        from the discovered interface means a parent agent driving this one cannot fabricate a
        callback result on its child's behalf.

        The resolution event is published HERE (the resolution site), mirroring
        :meth:`_resolve_and_publish` — so the ``ok``/``error`` :class:`CallbackResolved` is
        emitted in the order results actually arrive. The timeout/close case, which no update
        resolves, is published by the gate itself (:meth:`await_callback_result`)."""
        entry = self._status.callback_entry(result.tool_id)
        assert entry is not None  # guaranteed by the validator
        if result.error is not None:
            self._status.resolve_callback(
                result.tool_id, outcome="error", result=None, error=result.error
            )
        else:
            # Re-validate to obtain the coerced output object; the validator already confirmed
            # it matches, so this cannot raise here.
            validated = entry.output_adapter.validate_python(result.result)
            self._status.resolve_callback(
                result.tool_id, outcome="ok", result=validated, error=None
            )
        self._publish_callback_resolved(result.tool_id)
        return CallbackResultAck(tool_id=result.tool_id, accepted=True)

    def _publish_callback_resolved(self, tool_id: str) -> None:
        """Publish the :class:`CallbackResolved` for an already-resolved entry, against the turn
        that owns the call (not the workflow's notion of the "current" turn — an update handler
        resolving a callback isn't bound to any one turn)."""
        entry = self._status.callback_entry(tool_id)
        if entry is None:
            return
        self._pub(
            entry.turn_id,
            entry.turn_number,
            CallbackResolved(
                tool_id=entry.tool_id,
                tool_name=entry.tool_name,
                outcome=entry.outcome,
                error=entry.error,
            ),
        )

    async def await_callback_result(
        self,
        *,
        tool_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        output_adapter: TypeAdapter[Any],
        output_schema: dict[str, Any],
        timeout: timedelta | None,
    ) -> Any:
        """Park the current (callback) tool call until an external client supplies its result.

        The generic body of every ``@agent.callback_tool_defn`` tool. Runs IN-WORKFLOW inside
        the tool's :func:`tool_defn` wrapper — so it sits AFTER the approval gate and the
        ``tool_start`` publish — and:

          * registers the call as PENDING (also exposed via ``agent_status.pending_callbacks``);
          * publishes :class:`CallbackRequested` (args + the expected ``output_schema``) so a
            client can fulfill it on its own machine;
          * waits — on a ``wait_condition`` so no activity timeout is consumed — for a
            ``provide_callback_result`` update, an optional ``timeout``, or agent close.

        On an ``ok`` result it returns the client's value (already validated + coerced to the
        declared output type by the update handler). On a client-reported error, a timeout, or
        close-while-pending it raises :class:`CallbackToolError`, which the surrounding
        :func:`tool_defn` wrapper turns into a ``tool_error`` and surfaces to the model as an
        error result — so a failed callback does not crash the turn.
        """
        ctx = self.current_stream_context
        if ctx is None:
            raise RuntimeError(
                f"callback tool {tool_name!r} has no active turn to await a result against"
            )
        self._status.register_pending_callback(
            tool_id,
            tool_name,
            tool_input,
            output_schema,
            ctx.turn_number,
            ctx.turn_id,
            output_adapter,
        )
        self._pub(
            ctx.turn_id,
            ctx.turn_number,
            CallbackRequested(
                tool_id=tool_id,
                tool_name=tool_name,
                tool_input=tool_input,
                output_schema=output_schema,
            ),
        )
        timed_out = False
        try:
            await workflow.wait_condition(
                lambda: self._status.is_callback_resolved(tool_id) or self._closed,
                timeout=timeout,
            )
        except TimeoutError:
            # ``workflow.wait_condition(timeout=...)`` raises asyncio.TimeoutError (== builtin
            # TimeoutError on 3.11+) when the deadline elapses with the condition still false.
            timed_out = True

        # CAUSAL ORDERING (mirrors the approval gate): the ok/error CallbackResolved is published
        # at the resolution site (the update handler), in the order results arrive. The gate
        # publishes ONLY the timeout/close case — the one no update resolved.
        if not self._status.is_callback_resolved(tool_id):
            outcome = self._status.finalize_callback(
                tool_id, closed=self._closed, timed_out=timed_out
            )
            self._publish_callback_resolved(tool_id)
        else:
            outcome = self._status.finalize_callback(tool_id, closed=self._closed)

        if outcome.outcome == "ok":
            return outcome.result
        raise CallbackToolError(tool_name, outcome.error)

    def _handle_agent_status(self) -> AgentStatus:
        return self._status.to_agent_status()

    def _handle_agent_interface(self) -> list[AcceptedFunction]:
        """Answer the discovery query with the agent's callable surface, tool-style.

        One :class:`AcceptedFunction` per ``@agent.accepts`` handler — name, docstring
        description, and the input/output JSON schemas — all reachable through
        ``send_agent_message``. Operator-only channels are INTENTIONALLY absent here:
        ``tool_approval`` is a human-in-the-loop guardrail, and ``slash`` carries runtime
        operator controls such as approval-policy changes. A parent agent introspecting
        this contract must not be able to reach either one as an agent-to-agent tool."""
        return [
            AcceptedFunction(
                name=h.name,
                description=h.description,
                parameters=h.input_type.model_json_schema(),
                output=h.output_type.model_json_schema(),
            )
            for h in self._handlers.values()
            if h.name != _SLASH_MESSAGE_TYPE
        ]

    def _handle_operator_interface(self) -> list[OperatorCommand]:
        """Answer the operator discovery query with slash-command metadata.

        Unlike ``agent_interface``, this surface is for human/client control planes. It is
        intentionally not consumed by generated subagent tools.
        """
        return [definition.command for definition in self._slash_commands]

    def _operator_command_label(self, command_name: str) -> str:
        definition = self._find_slash_command(command_name)
        if definition is not None:
            return definition.command.label
        return f"/{command_name}"

    def _find_slash_command(self, command_name: str) -> SlashCommandDefinition | None:
        for definition in self._slash_commands:
            if definition.matches(command_name):
                return definition
        return None

    def _slash_command_context(self) -> SlashCommandContext:
        return SlashCommandContext(
            current_status=self.current_status,
            current_approval_policy=self.current_approval_policy,
            set_approval_policy=self.set_approval_policy,
            close=self._handle_close,
        )

    @property
    def current_stream_context(self) -> TurnStreamContext | None:
        """Carrier identifying the in-flight turn for activity-side publishing.

        Read by code dispatching activities that need to publish back
        to the workflow's stream (e.g. Gemini's streaming request path).
        Threaded through opaquely — see :class:`TurnStreamContext`.
        """
        return self._status.current_stream_context

    def _handle_close(self) -> None:
        self._closed = True

    # -- Turn loop ----------------------------------------------------------

    async def run(self, agent: object) -> None:
        """Drive the agent's turn loop to completion — the agent's ``@workflow.run`` body::

            @workflow.run
            async def run(self, config: AgentConfig) -> None:
                await self._runner.run(self)

        Waits for each inbound message, routes it to the matching ``@agent.accepts``
        handler on ``agent`` (the validator has already coerced the payload + rejected
        unknown functions), ``await``s the handler, and publishes its return value as the
        turn's reply. Turns are processed **sequentially** (one handler awaited to
        completion before the next), so the runner's notion of the active turn — and thus
        activity-side stream publishing — is unambiguous throughout a handler.

        Lifecycle events are emitted automatically: ``turn_started`` before the handler,
        then ``reply`` (success) or ``error`` (the handler raised), and always ``turn_end``
        — the single reliable end-of-turn signal — before looping. A handler that raises
        does NOT end the session: its error surfaces as an :class:`AgentError` and the loop
        continues with the next message.
        """
        while not self._closed:
            await workflow.wait_condition(
                lambda: self._status.has_pending_turns or self._closed
            )
            if self._closed:
                break
            envelope, turn_id = self._status.start_next_turn()
            turn_number = self._status.current_turn
            self._pub(
                turn_id, turn_number, TurnStarted(user_message=_render_message(envelope))
            )
            try:
                result = await self._dispatch_turn(agent, envelope)
                self._pub(
                    turn_id,
                    turn_number,
                    AgentReply(output=result.model_dump(mode="json")),
                )
            except Exception as e:  # noqa: BLE001 — surface ANY turn failure, keep the loop alive
                self._pub(turn_id, turn_number, AgentError(message=str(e)))
            finally:
                # The turn is over and the agent is idle again — whether the handler
                # returned or raised. Mark idle, then announce turn_end (the definitive
                # end-of-turn signal) before looping back to wait for the next message.
                self._status.complete_turn()
                self._pub(turn_id, turn_number, TurnEnded())

    async def _dispatch_turn(self, agent: object, envelope: AgentMessage) -> BaseModel:
        """Dispatch one already-validated turn envelope and return its reply model."""
        if envelope.type == _SLASH_MESSAGE_TYPE:
            command = SlashCommand.model_validate(envelope.payload)
            reply = self._handle_slash_command(command)
            if reply is not None:
                return reply
            handler = self._handlers.get(_SLASH_MESSAGE_TYPE)
            if handler is None:
                return TextReply(text=f"Unknown slash command: `{command.name}`.")
        else:
            handler = self._handlers[envelope.type]

        arg = handler.input_type.model_validate(envelope.payload)
        result = await getattr(agent, handler.name)(arg)
        if not isinstance(result, handler.output_type):
            raise ApplicationError(
                f"handler {handler.name!r} returned {type(result).__name__}, "
                f"expected {handler.output_type.__name__}",
                type="BadHandlerReturn",
                non_retryable=True,
        )
        return result

    def _handle_slash_command(self, command: SlashCommand) -> TextReply | None:
        definition = self._find_slash_command(command.name)
        if definition is None:
            return None
        return definition.execute(self._slash_command_context(), command)

    # -- Subagents ----------------------------------------------------------
    #
    # The runner-side surface the generated subagent toolset calls. The generated
    # ``start_<key>`` / ``stop_<key>`` / ``send_<function>`` tools are inline ``tool_defn``
    # closures that resolve the live runner from the ambient ``_CURRENT_RUNNER`` (set by
    # ``run_tool`` for the duration of every tool call) and delegate here — so all subagent
    # state lives on the runner, mutated deterministically in-workflow, with no holder object
    # or ``has_self`` plumbing.

    def _fresh_subagent_handle(self) -> str:
        """A short, TREE-UNIQUE handle for a new subagent: this agent's own id plus one fresh
        ``AGENT_ID_LENGTH``-char hex segment (e.g. ``<this agent's id>-a1b2c3``).

        The model references subagents by this handle (cheap to reproduce), never by the full child
        ``workflow_id``. It is ALSO pushed down as the child's ``AgentConfig.agent_id`` (see
        :meth:`start_subagent`), so the child stamps this same id on its own events — letting a
        client merging the streams label and group the child's events by the handle the parent knows
        it by, with no risk of two agents in the tree sharing an id.

        Tree-uniqueness rests on the reroll below: the fresh segment is regenerated until the full
        handle is unused in THIS agent's registry, so this agent's children all get distinct
        segments; prefixing with this agent's own (already tree-unique) id then extends uniqueness
        across the entire subagent tree. The reroll is LOAD-BEARING for that guarantee — do not drop
        it. ``workflow.uuid4`` is deterministic in-workflow (offline unit tests patch it)."""
        while True:
            candidate = f"{self._agent_id}-{workflow.uuid4().hex[:AGENT_ID_LENGTH]}"
            if not self._status.has_subagent(candidate):
                return candidate

    async def start_subagent(
        self,
        agent_key: str,
        workflow_type: str,
        task_queue: str,
        config: AgentConfig | None = None,
    ) -> str:
        """Start a child agent workflow as a subagent and register it; return its short handle.

        Mirrors ``session_manager.create_session`` — launches the registered ``workflow_type``
        on ``task_queue`` with a standardized :class:`AgentConfig` — but tracks the child in
        this runner's subagent registry instead. Returns a short ``handle`` (not the long child
        ``workflow_id``) for the model to address THIS instance in later ``send_<function>`` /
        ``stop_<key>`` calls (a parent may run several instances of one ``agent_key``); the
        workflow-side resolves ``handle`` → ``workflow_id`` internally."""
        handle = self._fresh_subagent_handle()
        workflow_id = f"{agent_key}-subagent-{workflow.uuid4()}"
        # Push the handle down as the child's own agent_id so the child stamps it on every event
        # it publishes — unifying "the id the parent references this subagent by" with "the id on
        # the subagent's own stream", which is what lets a client merge the two streams coherently
        # (and, since the handle is tree-unique, group by agent_id without collisions). This is the
        # one config field the parent overrides per-child (everything else passes through
        # unchanged); a caller-supplied agent_id would not match the parent's handle.
        child_config = (config if config is not None else AgentConfig()).model_copy(
            update={"agent_id": handle}
        )
        await workflow.start_child_workflow(
            workflow_type,
            child_config,
            id=workflow_id,
            task_queue=task_queue,
            # EXPLICIT: a subagent is owned by its parent and must never outlive it. If the
            # parent closes for ANY reason (its own `close` signal, completion, failure,
            # cancellation, or termination) before `stop_subagent` was called, the Temporal
            # server terminates this child. We pin TERMINATE rather than rely on the SDK
            # default so the guarantee can't silently change. (Graceful shutdown of a still-
            # wanted subagent is the explicit `stop_subagent` path, which sends `close`.)
            #
            # TODO: we may prefer to handle parent shutdown more gracefully than a hard
            # TERMINATE (which kills the child mid-turn with no cleanup — no `close` handling,
            # no chance to finalize in-flight work). Two candidate approaches:
            #   1. REQUEST_CANCEL — the server requests cancellation of the child on parent
            #      close, letting a child that handles cancellation tear down gracefully
            #      (requires the harness agent loop to treat cancellation as a clean stop).
            #   2. A workflow finalization/cleanup hook on the parent that, before it exits,
            #      stops every still-registered subagent through the SAME "front door" a
            #      human/UI uses — i.e. `stop_subagent` → the `close` signal — so children
            #      shut down via their normal graceful path rather than being killed by the
            #      server. (This keeps shutdown semantics uniform with manual stops, but must
            #      run on every parent-exit path, including failure/cancellation.)
            parent_close_policy=workflow.ParentClosePolicy.TERMINATE,
        )
        self._status.register_subagent(handle, workflow_id, agent_key)
        # Announce the subagent on this agent's stream (against the in-flight turn). The
        # ``workflow_id`` lets a consumer dynamically mount the subagent's own stream for a
        # consolidated view — subagent streams are never mirrored onto this one.
        self.publish(
            SubagentStarted(
                subagent_id=handle, agent_key=agent_key, workflow_id=workflow_id
            )
        )
        return handle

    async def stop_subagent(self, handle: str) -> None:
        """Signal a subagent to close and drop it from the registry.

        Raises ``UnknownSubagent`` if ``handle`` isn't one this agent started. Resolves the
        handle to the child ``workflow_id``, sends it the harness ``close`` signal (the same one
        a human/UI uses), publishes :class:`SubagentStopped` (so a consumer can unmount its
        stream), then deregisters so a later ``send_<function>`` to ``handle`` is rejected."""
        inst = self._status.subagent(handle)  # validate ownership (raises UnknownSubagent)
        await workflow.get_external_workflow_handle(inst.workflow_id).signal("close")
        self.publish(
            SubagentStopped(
                subagent_id=inst.handle,
                agent_key=inst.agent_key,
                workflow_id=inst.workflow_id,
            )
        )
        self._status.remove_subagent(handle)

    async def run_subagent_turn(
        self, handle: str, msg_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Drive one turn of a subagent (by its short ``handle``) and return the reply ``output``.

        The deterministic in-workflow half of a ``send_<function>`` tool: it resolves ``handle``
        to the child instance, serializes turns to it through that subagent's FIFO gate (so
        concurrent ``gather``-ed sends run in the model's call order, one at a time), then
        dispatches the single ``run_subagent_turn`` activity against the real child
        ``workflow_id`` with the now-exact ``expected_turn`` + resume ``from_offset``, and
        advances the local bookkeeping on completion.

        A ticket is taken **synchronously** (before the first ``await``) so gathered callers are
        ordered by call order, not await scheduling. Errors propagate as :class:`ApplicationError`
        (the tool layer renders them as an ``is_error`` result); the turn counter still advances
        for a turn the child accepted-but-errored, so the next send isn't spuriously stale."""
        inst = self._status.subagent(handle)  # raises UnknownSubagent
        ticket = inst.take_ticket()  # synchronous → FIFO admission in call order
        await workflow.wait_condition(
            lambda: inst.is_serving(ticket) or self._closed
        )
        if not inst.is_serving(ticket):
            # Woke on agent close while still queued behind an earlier turn.
            raise ApplicationError(
                f"agent closed before subagent {handle!r} turn {ticket} could run",
                type="AgentClosed",
                non_retryable=True,
            )
        try:
            expected = inst.next_expected_turn
            # The dispatch marker (SubagentMessageSent) is published by the activity itself,
            # WHEN it actually sends the message to the child — not here at execute_activity
            # dispatch time (there's a real gap before the activity runs). Mirrors how tool
            # activities publish tool_start from inside the activity. We pass the parent's turn
            # context + handle/agent_key so the activity can publish onto THIS agent's stream;
            # the activity's heartbeat memo dedupes the publish across retries (it only fires on
            # a fresh send, never on a heartbeat-resume).
            stream_context = self.current_stream_context
            if stream_context is None:
                raise ApplicationError(
                    "run_subagent_turn called with no active turn to publish against",
                    type="NoActiveTurn",
                    non_retryable=True,
                )
            try:
                result = await workflow.execute_activity(
                    RUN_SUBAGENT_TURN_ACTIVITY,
                    RunSubagentTurnInput(
                        child_workflow_id=inst.workflow_id,
                        type=msg_type,
                        payload=payload,
                        expected_turn=expected,
                        from_offset=inst.last_consumed_offset,
                        handle=inst.handle,
                        agent_key=inst.agent_key,
                        parent_stream_context=stream_context,
                    ),
                    start_to_close_timeout=DEFAULT_SUBAGENT_START_TO_CLOSE_TIMEOUT,
                    heartbeat_timeout=DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT,
                    result_type=SubagentTurnResult,
                )
            except ApplicationError as e:
                # A turn the child ACCEPTED but that then errored (or produced no reply) still
                # advanced the child's turn counter, so keep ours in lockstep — otherwise the
                # next send would be a spurious StaleTurn. A pre-acceptance rejection (abnormal
                # under the gate) advanced nothing, so leave the counter untouched.
                if e.type in ("SubagentTurnError", "SubagentNoReply"):
                    # Close the bracket on the child's ACTUAL accepted turn number — which the
                    # activity threads through the error details — NOT a re-derived ``expected``.
                    # The opening ``subagent_message_sent`` was published with that real number
                    # (``progress.turn_number``), and the close gate keys on
                    # ``(workflow_id, subagent_turn)``; using the same source on both sides makes
                    # the key match by construction rather than by an implicit
                    # validator+enqueue invariant. (They are equal today, but this can't drift.)
                    accepted_turn = self._accepted_turn_from_error(e, default=expected)
                    inst.next_expected_turn = accepted_turn + 1
                    # The child ran (and errored on) that turn — it still emitted its own
                    # turn_end — so we MUST close the [message_sent … reply_received] bracket on
                    # OUR stream, or a client merge would wedge waiting on a reply that never
                    # comes. (A pre-acceptance rejection ran no child turn → no bracket → no
                    # publish.) outcome="error" since this turn produced no usable reply.
                    self._publish_subagent_reply_received(
                        inst, msg_type, accepted_turn, outcome="error"
                    )
                raise
            inst.next_expected_turn = result.turn_number + 1
            inst.last_consumed_offset = result.consumed_offset
            # Close the bracket on OUR stream now that the agent (this workflow) actually holds
            # the reply — BEFORE ``release_gate()`` in the finally, so this turn's reply_received
            # is published ahead of the next gathered turn's message_sent (the merge relies on
            # per-subagent brackets never overlapping; see run_subagent_turn's FIFO gate).
            self._publish_subagent_reply_received(
                inst, msg_type, result.turn_number, outcome="ok"
            )
            return result.output
        finally:
            inst.release_gate()

    @staticmethod
    def _accepted_turn_from_error(e: ApplicationError, *, default: int) -> int:
        """The child's ACTUAL accepted turn number, threaded through the activity's error details.

        On an accepted-but-errored child turn the ``run_subagent_turn`` activity raises an
        ``ApplicationError`` carrying ``{"subagent_turn": <the child's real accepted turn>}`` — the
        SAME number the activity stamped on the opening ``subagent_message_sent``. We close the
        bracket on that exact key so the client merge's close gate (keyed on
        ``(workflow_id, subagent_turn)``) always matches, independent of the validator+enqueue
        invariant that makes it equal to ``default`` (``expected``) today. Falls back to ``default``
        if the detail is absent (older activity build / unexpected shape)."""
        for detail in e.details or ():
            if isinstance(detail, dict) and "subagent_turn" in detail:
                return int(detail["subagent_turn"])
        return default

    def _publish_subagent_reply_received(
        self,
        inst: _SubagentInstance,
        function: str,
        subagent_turn: int,
        *,
        outcome: Literal["ok", "error"],
    ) -> None:
        """Publish the :class:`SubagentReplyReceived` close marker for one subagent turn.

        Published IN-WORKFLOW (not from the activity) — the agent *is* the workflow, so
        "received" must mean the AGENT (this workflow) has the reply in hand, not merely that the
        ``run_subagent_turn`` activity (potentially on another machine) returned. In-workflow is
        also deterministic and needs no heartbeat dedup, unlike the activity-published
        ``subagent_message_sent`` (which must survive activity retries). Mirrors
        ``SubagentMessageSent``'s correlation fields so a client stream-merge can match the
        bracket on ``(workflow_id, subagent_turn)``.
        """
        self.publish(
            SubagentReplyReceived(
                subagent_id=inst.handle,
                agent_key=inst.agent_key,
                workflow_id=inst.workflow_id,
                function=function,
                subagent_turn=subagent_turn,
                outcome=outcome,
            )
        )

    def publish(self, event: AgentStreamItem) -> None:
        """Publish an event against the in-flight turn (for custom intermediate events).

        Most agents never need this — streaming (reply deltas, tool lifecycle) is handled
        by the runner↔SDK integration and ``run_tool``. Use it from inside a handler to
        emit a bespoke progress event. Raises if no turn is active.
        """
        ctx = self.current_stream_context
        if ctx is None:
            raise RuntimeError("publish() called with no active turn")
        self._pub(ctx.turn_id, ctx.turn_number, event)

    # -- Activity-side publishing helper -----------------------------------

    @staticmethod
    @asynccontextmanager
    async def publisher_from_activity(
        context: TurnStreamContext,
        *,
        batch_interval: timedelta = timedelta(milliseconds=50),
    ) -> AsyncIterator[TurnEventPublisher]:
        """Open a :class:`TurnEventPublisher` from inside a Temporal activity.

        Use from within a ``@activity.defn`` that needs to publish
        turn events (e.g. ``reply_delta`` chunks from a streaming model
        call) to its parent workflow's :class:`WorkflowStream`.

        Encapsulates the :class:`WorkflowStreamClient` lifecycle (entered
        for batched flushing, exited so the tail drains before the
        activity returns) and the topic binding. Activities just call
        ``publisher.publish(...)``.

        Args:
            context: Carrier identifying the workflow + turn to publish
                against. Built on the workflow side via
                :attr:`AgentWorkflowRunner.current_stream_context` and
                forwarded opaquely through activity inputs.
            batch_interval: Background flush cadence on the underlying
                stream client. Default 50ms keeps the UI feel snappy.

        Yields:
            A :class:`TurnEventPublisher` bound to the active workflow
            (resolved from the activity context) and the given turn.
        """
        client = WorkflowStreamClient.from_within_activity(
            batch_interval=batch_interval,
        )
        # ``from_within_activity`` targets the workflow that SCHEDULED this activity (always the
        # publishing agent), so events land on the right stream. The agent's SHORT id to stamp them
        # with is not derivable from ``activity.info()`` (which only knows the workflow_id), so it
        # rides in on the threaded ``context`` (TurnStreamContext.agent_id).
        async with client:
            yield TurnEventPublisher(
                events=client.topic(TURN_EVENTS_TOPIC, type=AgentEvent),
                context=context,
            )

    # -- Tool execution -----------------------------------------------------

    async def run_tool(
        self,
        call_id: str,
        tool_callable: Callable[..., Awaitable[Any]],
        /,
        *args: Any,
        injections: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Invoke one tool call, owning the per-call tool id for its lifecycle events.

        ``call_id`` is the stable id correlating every event of this invocation
        (``tool_requested`` → ``tool_start`` → ``tool_end``/``tool_error``). The same
        tool may be requested several times in one turn, each with its own id — which
        is why the id arrives here, per-invocation, rather than being baked into the
        tool when it is declared to the model. ``run_tool`` parks it in the ambient
        ``_CURRENT_TOOL_ID`` for the duration of the call, scoped to this asyncio
        Task's context (so concurrent calls, even of the same tool, never collide).

        ``injections`` supplies values for the tool's *injected* parameters (declared
        via :data:`Injected`) — keyed by parameter name. These are filled by the
        workflow, not the model, and are hidden from the model's tool schema; ``run_tool``
        parks them in the ambient ``_CURRENT_TOOL_INJECTIONS`` for the dispatch machinery
        to read (see :func:`_current_tool_injections`).

        ``tool_callable`` is whatever executes the tool and is responsible for
        publishing its own ``tool_start``/``tool_end`` events:

        * an **activity tool** — an :func:`activity_tool_defn` object, whose dispatcher
          calls :meth:`AgentToolContext.for_current_tool_id` to build the context it
          ferries into the activity, where the decorator's activity body publishes;
        * a **workflow tool** — a :func:`tool_defn` object, which runs inline and reads
          the ambient tool id directly to publish in-process.

        Either applies the agent's tool-approval policy first (gating the call when
        required); that gate lives in the in-workflow prologue, not here. ``run_tool``
        itself neither
        dispatches activities nor publishes — that lives in the tool object so
        ``tool_start`` means "now executing."
        """
        runner_token = _CURRENT_RUNNER.set(self)
        tool_id_token = _CURRENT_TOOL_ID.set(call_id)
        injections_token = _CURRENT_TOOL_INJECTIONS.set(injections)
        try:
            return await tool_callable(*args, **kwargs)
        finally:
            _CURRENT_TOOL_INJECTIONS.reset(injections_token)
            _CURRENT_TOOL_ID.reset(tool_id_token)
            _CURRENT_RUNNER.reset(runner_token)

    # -- Internal -----------------------------------------------------------

    def _pub(self, turn_id: str, turn_number: int, event: AgentStreamItem) -> None:
        """Wrap ``event`` in an :class:`AgentEvent` envelope and publish it."""
        self._events.publish(
            AgentEvent(
                event=event,
                # This agent's own short id — so every event on the stream self-identifies its
                # source agent for the client stream-merge (and a single-agent consumer can filter
                # by it). For a subagent this is the handle its parent references it by.
                agent_id=self._agent_id,
                turn_id=turn_id,
                turn_number=turn_number,
                timestamp=workflow.time(),
            )
        )


# ---------------------------------------------------------------------------
# Shared tool helpers — signature/input introspection used by both tool decorators
# ---------------------------------------------------------------------------


def _signature_with_tool_ctx(user_sig: inspect.Signature) -> inspect.Signature:
    """Return ``user_sig`` with a trailing ``tool_ctx: AgentToolContext = None`` param.

    This is the wrapper's runtime ``__signature__``, read by ``@activity.defn`` to
    build the activity's ``arg_types`` so Temporal serializes the context as the last
    argument. The param is POSITIONAL_OR_KEYWORD (Temporal forbids keyword-only
    activity args) and defaults to ``None`` so it can legally follow any of the user's
    own defaulted params. It is NOT part of the decorator's *static* type — the
    developer's call signature is preserved exactly (see ``_P``/``_R``).
    """
    params = list(user_sig.parameters.values())
    params.append(
        inspect.Parameter(
            "tool_ctx",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=None,
            annotation=AgentToolContext,
        )
    )
    return user_sig.replace(parameters=params)


def _tool_input(
    user_sig: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Best-effort dict of the user-facing args for a tool_start event's ``tool_input``."""
    try:
        bound = user_sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except TypeError:
        return {}


# ---------------------------------------------------------------------------
# @agent.activity_tool_defn / @agent.tool_defn — the tool decorators
# ---------------------------------------------------------------------------
#
# Two decorators define agent tools: ``activity_tool_defn`` (durable, activity-backed)
# and ``tool_defn`` (inline in the workflow). Each owns a single execution path and bakes
# the optional human-approval gate into the in-workflow prologue (NEVER the activity), so
# the wait can sit indefinitely without consuming the tool's activity timeout. ``run_tool``
# stays a thin per-call funnel that parks the ambient tool id / runner / injections these
# decorators read.


@dataclass
class _ToolSig:
    """The schema-facing + dispatch-facing views of a tool's signature, computed once
    at decoration time.

    ``model_sig`` / ``model_annotations`` are what the model's schema is built from —
    the user signature with ``self`` and every ``Injected[...]`` parameter removed.
    ``user_params`` / ``inject_names`` / ``has_self`` are what dispatch needs to rebuild
    the full call (model-supplied values + workflow-injected values) in the activity's
    own parameter order.
    """

    user_sig: inspect.Signature
    model_sig: inspect.Signature
    model_annotations: dict[str, Any]
    user_params: list[inspect.Parameter]
    inject_names: tuple[str, ...]
    has_self: bool
    return_type: Any  # the tool's declared return type, or None if unannotated


def _tool_signatures(user_fn: Callable[..., Any]) -> _ToolSig:
    user_sig = inspect.signature(user_fn)
    user_params = list(user_sig.parameters.values())
    inject_names = _injected_param_names(user_fn)
    has_self = bool(user_params) and user_params[0].name == "self"
    hidden = set(inject_names) | ({"self"} if has_self else set())
    model_sig = user_sig.replace(
        parameters=[p for p in user_params if p.name not in hidden]
    )
    model_annotations = {
        k: v
        for k, v in getattr(user_fn, "__annotations__", {}).items()
        if k not in hidden
    }
    # Resolve the declared return type so an activity tool's dispatcher can ask
    # ``execute_activity`` to reconstruct the typed result. Dispatching by activity NAME
    # otherwise yields a raw dict for a model/dataclass return; this keeps the dispatcher
    # honoring its own ``-> _R`` signature (resolves string annotations too).
    try:
        return_type = get_type_hints(user_fn).get("return")
    except Exception:
        ret = user_sig.return_annotation
        return_type = None if ret is inspect.Signature.empty else ret
    return _ToolSig(
        user_sig=user_sig,
        model_sig=model_sig,
        model_annotations=model_annotations,
        user_params=user_params,
        inject_names=inject_names,
        has_self=has_self,
        return_type=return_type,
    )


def _apply_model_facing_views(wrapper: Any, user_fn: Callable[..., Any], sig: _ToolSig,
                              tool_name: str) -> None:
    """Stamp the wrapper with the MODEL-facing name/doc/signature/annotations.

    Deliberately does NOT set ``__wrapped__``: the schema builder (the plugin's
    ``function_param``) reads these views directly, and a ``__wrapped__`` pointing at the
    user fn would re-expose the hidden (``self`` / ``Injected[...]``) parameters to the
    model. The static type stays the developer's own via the decorator's return cast."""
    wrapper.__name__ = tool_name
    wrapper.__qualname__ = tool_name
    wrapper.__doc__ = user_fn.__doc__
    wrapper.__module__ = user_fn.__module__
    wrapper.__signature__ = sig.model_sig
    wrapper.__annotations__ = sig.model_annotations


def activity_tool_defn(
    *,
    inherently_safe: bool = False,
    activity_config: ActivityConfig | None = None,
    name: str | None = None,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Define a durable, activity-backed agent tool::

        @agent.activity_tool_defn(
            activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=2)),
        )
        async def delete_workflow(store_display_name: Injected[str], workflow_id: str) -> str:
            ...

    The returned object is the **in-workflow dispatcher** — call it (the model's
    function call resolves to it via ``run_tool``) and it binds the model args, applies the
    agent's tool-approval policy (gating the call when required; see
    :func:`_apply_approval_policy`), fills ``Injected[...]`` params from the ambient
    injections, and executes the tool as a Temporal activity. Its durable body — the
    auto-``@activity.defn``-wrapped function that publishes ``tool_start``/``tool_end``
    from inside the running activity — is obtained with :func:`tool_activity` for worker
    registration::

        Worker(..., activities=[tool_activity(delete_workflow), ...])

    ``name`` overrides the activity name (default: the function's ``__name__``).

    ``inherently_safe=True`` asserts this tool is *never*, under any input, unsafe — a
    static hint, NOT a decision to skip approval. Whether it is actually gated is up to
    the agent's :class:`ToolApprovalPolicy` (the safe-by-default baseline gates everything;
    only a policy that opts into ``auto_approve_inherently_safe`` lets a safe tool through).
    Mark a tool safe ONLY if it is *always* safe; if it is even sometimes unsafe, leave it
    ``False`` (the default).
    """

    def decorator(user_fn: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
        sig = _tool_signatures(user_fn)
        tool_name = name or user_fn.__name__

        # ---- activity body: runs in the worker, publishes lifecycle from within ----
        async def activity_body(*args: Any, **kwargs: Any) -> Any:
            *user_args, tool_ctx = args
            if not isinstance(tool_ctx, AgentToolContext):
                raise RuntimeError(
                    f"agent tool {tool_name!r} ran as an activity without an "
                    f"AgentToolContext trailing argument"
                )
            full_input = _tool_input(sig.user_sig, tuple(user_args), kwargs)
            tool_input = {
                k: v
                for k, v in full_input.items()
                if k not in sig.inject_names and k != "self"
            }
            async with AgentWorkflowRunner.publisher_from_activity(
                tool_ctx.stream_context
            ) as pub:
                pub.publish(
                    ToolStartEvent(
                        tool_id=tool_ctx.tool_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                    )
                )
                try:
                    result = await user_fn(*user_args, **kwargs)
                except Exception as e:
                    pub.publish(
                        ToolErrorEvent(
                            tool_id=tool_ctx.tool_id, tool_name=tool_name, message=str(e)
                        )
                    )
                    raise
                pub.publish(
                    ToolEndEvent(
                        tool_id=tool_ctx.tool_id,
                        tool_name=tool_name,
                        tool_output=str(result),
                    )
                )
                return result

        activity_body.__name__ = tool_name
        activity_body.__qualname__ = tool_name
        activity_body.__doc__ = user_fn.__doc__
        activity_body.__module__ = user_fn.__module__
        activity_body.__signature__ = _signature_with_tool_ctx(sig.user_sig)  # type: ignore[attr-defined]
        activity_body.__annotations__ = {
            **getattr(user_fn, "__annotations__", {}),
            "tool_ctx": AgentToolContext,
        }
        the_activity = activity.defn(name=tool_name)(activity_body)

        config: ActivityConfig = {
            **(
                ActivityConfig(start_to_close_timeout=timedelta(seconds=30))
                if activity_config is None
                else activity_config
            )
        }
        if "summary" not in config:
            config["summary"] = "tool_call"

        # ---- dispatcher: runs in-workflow (gate + execute_activity) ----
        async def dispatch(*args: Any, **kwargs: Any) -> Any:
            bound = sig.model_sig.bind(*args, **kwargs)
            bound.apply_defaults()
            model_input = dict(bound.arguments)
            await _apply_approval_policy(
                tool_name, model_input, inherently_safe=inherently_safe
            )

            injections = _current_tool_injections() if sig.inject_names else {}
            activity_args: list[Any] = []
            for p in sig.user_params:
                if sig.has_self and p.name == "self":
                    continue
                if p.name in sig.inject_names:
                    if p.name not in injections:
                        raise ApplicationError(
                            f"tool {tool_name!r} requires injected argument "
                            f"{p.name!r}, but run_tool was called without it",
                            type="MissingInjection",
                            non_retryable=True,
                        )
                    activity_args.append(injections[p.name])
                else:
                    activity_args.append(bound.arguments[p.name])
            activity_args.append(AgentToolContext.for_current_tool_id())
            # Dispatch by activity NAME, so pass result_type explicitly — otherwise a
            # model/dataclass return comes back as a raw dict (Temporal can't infer the
            # type from a name the way it would from a function reference).
            return await workflow.execute_activity(
                tool_name,
                args=activity_args,
                result_type=sig.return_type,
                **config,
            )

        _apply_model_facing_views(dispatch, user_fn, sig, tool_name)
        dispatch.activity = the_activity  # type: ignore[attr-defined]
        dispatch.__agent_activity_tool__ = True  # type: ignore[attr-defined]
        return cast("Callable[_P, Awaitable[_R]]", dispatch)

    return decorator


def tool_activity(tool: Callable[..., Any]) -> Callable[..., Any]:
    """Return the registrable Temporal activity for an :func:`activity_tool_defn` tool.

    The decorator returns the in-workflow *dispatcher*, typed as the developer's own
    callable so the model-facing call signature is preserved; its durable activity body
    lives on a ``.activity`` attribute that is invisible to type checkers. Use this to
    register the activity on a worker without a per-call ``# type: ignore``::

        Worker(..., activities=[tool_activity(get_page_outline), tool_activity(read_section)])

    Raises :class:`TypeError` if ``tool`` was not produced by :func:`activity_tool_defn`
    (e.g. a :func:`tool_defn` inline tool, which has no activity, or a plain function).
    """
    activity_fn = getattr(tool, "activity", None)
    if not getattr(tool, "__agent_activity_tool__", False) or activity_fn is None:
        name = getattr(tool, "__name__", None) or repr(tool)
        raise TypeError(
            f"{name} is not an @agent.activity_tool_defn tool, so it has no activity to "
            f"register; tool_activity() requires a tool defined with "
            f"@agent.activity_tool_defn (got {type(tool).__name__})."
        )
    return cast("Callable[..., Any]", activity_fn)


def tool_defn(
    *, inherently_safe: bool = False
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Define an agent tool that runs INLINE in the workflow (no activity)::

        @agent.tool_defn()
        async def summarize(text: str) -> str: ...

        @agent.tool_defn(inherently_safe=True)
        async def lookup(change: Injected[Change], note: str) -> str: ...

    The returned object is invoked via ``run_tool`` within an active turn; it applies the
    agent's tool-approval policy (gating the call when required; see
    :func:`_apply_approval_policy`), publishes ``tool_start``/``tool_end`` (or
    ``tool_error``) in-process, and fills ``Injected[...]`` parameters from the ambient
    injections. ``inherently_safe`` is the same static safety hint as on
    :func:`activity_tool_defn` (the policy, not the tool, decides enforcement). Use for
    deterministic, side-effect-free-ish work that belongs in the workflow itself; reach for
    :func:`activity_tool_defn` when the work must cross into an activity (I/O,
    nondeterminism, long-running).
    """

    def decorator(user_fn: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
        sig = _tool_signatures(user_fn)
        tool_name = user_fn.__name__

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not workflow.in_workflow():
                raise RuntimeError(
                    f"workflow tool {tool_name!r} was invoked outside a workflow"
                )
            runner = _CURRENT_RUNNER.get()
            ctx = runner.current_stream_context if runner is not None else None
            tool_id = _CURRENT_TOOL_ID.get()
            if runner is None or ctx is None or tool_id is None:
                raise RuntimeError(
                    f"workflow tool {tool_name!r} must be invoked via run_tool within "
                    f"an active turn"
                )
            available = _current_tool_injections()
            try:
                inject_kwargs = {n: available[n] for n in sig.inject_names}
            except KeyError as missing:
                raise RuntimeError(
                    f"workflow tool {tool_name!r} requires injected argument {missing} "
                    f"but run_tool was called without it in injections="
                ) from None

            model_input = _tool_input(sig.model_sig, args, kwargs)
            await _apply_approval_policy(
                tool_name, model_input, inherently_safe=inherently_safe
            )

            runner._pub(
                ctx.turn_id,
                ctx.turn_number,
                ToolStartEvent(
                    tool_id=tool_id, tool_name=tool_name, tool_input=model_input
                ),
            )
            try:
                result = await user_fn(*args, **inject_kwargs, **kwargs)
            except Exception as e:
                runner._pub(
                    ctx.turn_id,
                    ctx.turn_number,
                    ToolErrorEvent(
                        tool_id=tool_id, tool_name=tool_name, message=str(e)
                    ),
                )
                raise
            runner._pub(
                ctx.turn_id,
                ctx.turn_number,
                ToolEndEvent(
                    tool_id=tool_id, tool_name=tool_name, tool_output=str(result)
                ),
            )
            return result

        _apply_model_facing_views(wrapper, user_fn, sig, tool_name)
        wrapper.__agent_tool__ = True  # type: ignore[attr-defined]
        return cast("Callable[_P, Awaitable[_R]]", wrapper)

    return decorator


def _assert_callback_stub(stub_fn: Callable[..., Any]) -> None:
    """Enforce that a ``@callback_tool_defn`` target is an ``async def`` whose body is exactly
    ``...``.

    A callback tool declares only a *contract* — the harness supplies the body and an attached
    client fulfills the call — so a real body would be dead code that never runs and would
    mislead the author into thinking it executes. This parses the decorated function's source
    and rejects anything but a lone ``...`` (an optional leading docstring is allowed). If the
    source can't be read (e.g. a dynamically built function), the check is skipped rather than
    failing — best-effort enforcement of the intended shape.
    """
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(stub_fn)))
    except (OSError, TypeError, SyntaxError):
        return  # source unavailable — can't verify the stub shape; skip.
    node = next(
        (n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
        None,
    )
    if node is None:
        return
    name = stub_fn.__name__
    if not isinstance(node, ast.AsyncFunctionDef):
        raise TypeError(
            f"@agent.callback_tool_defn {name!r} must be declared `async def` — a callback "
            f"tool is awaited like every other tool."
        )
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # a leading docstring is allowed (it's the tool description)
    is_ellipsis = (
        len(body) == 1
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and body[0].value.value is Ellipsis
    )
    if not is_ellipsis:
        raise TypeError(
            f"@agent.callback_tool_defn {name!r} must have a body of exactly `...` — the "
            f"harness supplies the implementation and an attached client fulfills the call. "
            f"Declare only the signature (parameters + return type) and a docstring."
        )


def callback_tool_defn(
    *,
    inherently_safe: bool = False,
    name: str | None = None,
    timeout: timedelta | None = None,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    r"""Define a CALLBACK tool — one whose work runs on an EXTERNAL CLIENT, not the worker.

    Use it when a tool needs state or an environment the agent's worker does not have — e.g. a
    cloud-hosted coding agent reading a file on the *user's laptop*, or an agent asking the
    user's phone to capture and upload a photo. The author declares only the tool's contract; the
    body must be exactly ``...`` (enforced at import) — the harness provides a single generic
    implementation and an attached client fulfills each call on its own machine::

        class FileContents(BaseModel):
            path: str
            text: str

        @agent.callback_tool_defn()
        async def read_file(path: str) -> FileContents:
            \"\"\"Read a file from the user's local machine.\"\"\"
            ...   # never runs; the attached client executes it and returns the result

    The returned object is a normal inline tool: it is invoked via ``run_tool`` within an active
    turn and goes through the EXACT SAME path as every other tool — the agent's
    :class:`ToolApprovalPolicy` gates it identically (a callback tool is not auto-anything; the
    policy decides), and it publishes ``tool_start`` / ``tool_end`` / ``tool_error``. The only
    difference is what happens between start and end: instead of running a body, it publishes a
    :class:`CallbackRequested` event (the args + the JSON schema of the declared return type) and
    parks on an in-workflow wait condition until a client submits the result via the
    ``provide_callback_result`` update (see :class:`CallbackResult`). The client's payload is
    validated against the return type before it becomes the tool's value; a client-reported error
    or a timeout surfaces to the model as a tool error rather than crashing the turn.

    ``name`` overrides the tool name (default: the function's ``__name__``). ``timeout`` bounds
    the wait for a result (``None`` = wait indefinitely, durably — no activity timeout is
    consumed). ``inherently_safe`` is the same static safety hint as on the other tool decorators
    (the policy, not the tool, decides enforcement).

    Because it is an ordinary harness tool, a callback tool composes with Code Mode and subagent
    toolsets for free.
    """

    def decorator(stub_fn: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
        _assert_callback_stub(stub_fn)
        tool_name = name or stub_fn.__name__
        sig = _tool_signatures(stub_fn)
        if sig.return_type is None:
            raise TypeError(
                f"@agent.callback_tool_defn {tool_name!r} must declare a concrete return type "
                f"(the client's result is validated against it); got no return annotation."
            )
        output_adapter: TypeAdapter[Any] = TypeAdapter(sig.return_type)
        output_schema = output_adapter.json_schema()

        async def _impl(*args: Any, **kwargs: Any) -> Any:
            # Reconstruct the model-facing args to hand to the fulfilling client: bind against the
            # full user signature (so injected params don't break the bind), then drop the hidden
            # ones — the same tool_input the surrounding tool_start carries.
            full_input = _tool_input(sig.user_sig, args, kwargs)
            tool_input = {
                k: v
                for k, v in full_input.items()
                if k not in sig.inject_names and k != "self"
            }
            return await _current_runner().await_callback_result(
                tool_id=_current_tool_id(),
                tool_name=tool_name,
                tool_input=tool_input,
                output_adapter=output_adapter,
                output_schema=output_schema,
                timeout=timeout,
            )

        # Carry the author's contract onto the generated impl so tool_defn's introspection
        # (signature → model schema, docstring → description, return type) reads the stub, not
        # this generic *args/**kwargs shim.
        _impl.__name__ = tool_name
        _impl.__qualname__ = tool_name
        _impl.__doc__ = stub_fn.__doc__
        _impl.__module__ = stub_fn.__module__
        _impl.__signature__ = sig.user_sig  # type: ignore[attr-defined]
        _impl.__annotations__ = dict(getattr(stub_fn, "__annotations__", {}))

        # Route through the standard inline-tool path so the callback tool gets the SAME
        # approval gate and tool_start/tool_end/tool_error publishing as any other tool. The
        # policy engine cannot tell — and does not need to tell — a callback tool apart.
        return tool_defn(inherently_safe=inherently_safe)(
            cast("Callable[_P, Awaitable[_R]]", _impl)
        )

    return decorator
