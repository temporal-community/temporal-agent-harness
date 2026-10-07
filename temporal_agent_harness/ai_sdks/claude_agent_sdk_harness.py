# ABOUTME: Harness glue for the Claude Agent SDK Temporal plugin (temporalio.claude_agent_sdk,
# temporalio/ai-integrations#33). EXPLORATORY: written against that PR's head commit, before it is
# merged. `HarnessClaudeAgent` is a `DurableClaudeAgent` whose tool execution is handed to the
# harness runner, so the harness (not the plugin) owns approvals, tool lifecycle events and
# activity-backed tool dispatch. The plugin keeps owning what it is good at: the model segment
# Activities, the deferred-call checkpoints, the conversation held in the Workflow, Continue-As-New.
#
# It overrides TWO private members of DurableClaudeAgent (`_execute_tool`, `_publish`). See
# docs/design/claude-agent-sdk-integration.md for the seams to ask upstream for so that this
# subclass can disappear.

"""Harness integration for the Claude Agent SDK (``temporalio.claude_agent_sdk``).

>>> from temporal_agent_harness.ai_sdks.claude_agent_sdk_harness import HarnessClaudeAgent
>>> claude = HarnessClaudeAgent(
...     runner,                       # the AgentWorkflowRunner
...     system_prompt="You are a refund assistant.",
...     tools=[look_up_order, issue_refund],   # @agent.tool_defn / @agent.activity_tool_defn
... )
>>> answer = await claude.run(message.text)

Harness tools are exposed to Claude as custom tools; every call goes through
``runner.run_tool`` so the agent's ``ToolApprovalPolicy`` gates it and ``tool_start`` /
``tool_end`` / ``tool_error`` reach the turn stream. Claude Code's own tools that the plugin
runs as Activities (``Bash``, ``mcp__*``) get the same gate and events through
:func:`_apply_approval_policy`, exactly like :func:`as_harness_mcp_server` does for the OpenAI
Agents SDK. The plugin's own ``needs_approval`` / ``tool_approvals`` / ``decide`` machinery is
switched off: there is one approval path, the harness's.

.. warning::
    Experimental. Depends on an unmerged upstream PR and on two private members of its
    ``DurableClaudeAgent``.
"""

from __future__ import annotations

import fnmatch
import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ForwardRef, get_type_hints

from pydantic import create_model
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_protocol import (
    ToolEndEvent,
    ToolErrorEvent,
    ToolStartEvent,
)
from temporal_agent_harness.harness.agent_workflow import (
    AgentWorkflowRunner,
    ToolApprovalDenied,
    _apply_approval_policy,
)

try:
    from temporalio.claude_agent_sdk import (
        DeferredCall,
        DurableClaudeAgent,
        DurableTool,
        ToolOutcome,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without the extra
    raise RuntimeError(
        "Claude Agent SDK support requires `temporalio-claude-agent-sdk` "
        "(temporalio/ai-integrations#33, not yet released) and temporalio>=1.33. "
        "Install it with the `claude-agent-sdk` extra."
    ) from exc

__all__ = ["HarnessClaudeAgent", "as_claude_tool", "as_claude_tools"]

_HARNESS_TOOL_ATTRS = ("__agent_tool__", "__agent_activity_tool__")

# DurableClaudeAgent's own default for ``tool_activities``; used to detect name clashes when the
# caller does not override it.
_DEFAULT_TOOL_ACTIVITIES = ("Bash", "mcp__*")

# Same wording the plugin gives a rejected call, so Claude reacts to a denial identically
# whichever layer produced it.
_DENIED = "A human reviewer rejected this action. Do not retry it."


def as_claude_tool(tool_callable: Callable[..., Awaitable[Any]]) -> DurableTool:
    """Describe a harness tool to Claude as a :class:`DurableTool`.

    The returned object only carries the *spec* (name, description, JSON schema) the plugin
    advertises to Claude. It is never executed by the plugin: :class:`HarnessClaudeAgent`
    routes the call to ``runner.run_tool(tool_callable, ...)`` instead, and keeps the callable
    in its own table keyed by name.
    """
    _require_harness_tool(tool_callable)
    name = tool_callable.__name__
    return DurableTool(
        # Not a Temporal activity: the plugin would only touch this field in the branch that
        # HarnessClaudeAgent._execute_tool replaces.
        activity=tool_callable,
        name=name,
        description=inspect.getdoc(tool_callable) or name,
        input_schema=_input_schema(tool_callable),
        needs_approval=False,  # the harness policy gates, never the plugin
    )


def as_claude_tools(
    tool_callables: Sequence[Callable[..., Awaitable[Any]]],
) -> list[DurableTool]:
    """Describe several harness tools to Claude. See :func:`as_claude_tool`."""
    return [as_claude_tool(t) for t in tool_callables]


class HarnessClaudeAgent(DurableClaudeAgent):
    """A :class:`DurableClaudeAgent` whose tool calls run under the harness runner.

    Takes the runner and harness tools instead of ``DurableTool`` objects; every other
    argument is forwarded unchanged (``builtin_tools``, ``tool_activities``, segment
    timeouts, Continue-As-New, ...). ``tools`` is positional-or-keyword here so call sites read
    like the other harness adapters.

    ``tool_approvals``, ``approvers`` and the plugin's ``decide`` / ``validate_decision`` /
    ``pending_approvals`` are not used: the harness's ``tool_approval`` update and
    ``ToolApprovalRequested`` events cover them. Passing ``tool_approvals`` raises.
    """

    def __init__(
        self,
        runner: AgentWorkflowRunner,
        *,
        tools: Sequence[Callable[..., Awaitable[Any]]] = (),
        tool_approvals: Sequence[str] = (),
        approvers: Sequence[str] | None = None,
        inherently_safe_builtins: Sequence[str] = (),
        **kwargs: Any,
    ) -> None:
        """Create the agent, in the Workflow's ``__init__`` or inside a message handler.

        Args:
            runner: The agent's :class:`AgentWorkflowRunner`.
            tools: Harness tools (``@agent.tool_defn`` / ``@agent.activity_tool_defn``).
            tool_approvals: Refused. Configure ``ToolApprovalPolicy`` on the runner.
            approvers: Refused, for the same reason.
            inherently_safe_builtins: Patterns of Claude Code tools (``Bash``, ``mcp__x__*``)
                that the policy may treat as ``inherently_safe`` (never gated by default).
                Everything else that runs as an Activity is safe-by-default gated.
            **kwargs: Forwarded to :class:`DurableClaudeAgent`.
        """
        if tool_approvals or approvers:
            raise ValueError(
                "HarnessClaudeAgent does not take tool_approvals/approvers: approvals are the "
                "harness's (ToolApprovalPolicy on the runner)."
            )
        # A harness tool must not share a name with a Claude Code tool: Claude could not tell
        # them apart, and the harness tool would shadow the engine's own.
        engine_names = [
            *kwargs.get("builtin_tools", ()),
            *kwargs.get("tool_activities", _DEFAULT_TOOL_ACTIVITIES),
        ]
        for t in tools:
            clash = next((p for p in engine_names if fnmatch.fnmatchcase(t.__name__, p)), None)
            if clash is not None:
                raise ValueError(
                    f"Harness tool {t.__name__!r} has the same name as the Claude Code tool "
                    f"pattern {clash!r}; rename the harness tool."
                )
        self._harness_runner = runner
        self._harness_tools = {t.__name__: t for t in tools}
        self._safe_builtins = list(inherently_safe_builtins)
        super().__init__(tools=as_claude_tools(tools), **kwargs)

    # ---- the seams (private upstream; see the design doc) ----

    async def _execute_tool(
        self, call: DeferredCall, record: dict[str, Any]
    ) -> ToolOutcome:
        runner = self._harness_runner
        # The plugin says which kind of call this is; trust it over the name.
        if call.kind == "engine":
            return await self._run_engine_tool(runner, call, record)
        tool = self._harness_tools.get(call.name)
        if tool is not None:
            return await self._run_harness_tool(runner, tool, call, record)
        record["status"] = "unknown tool"
        return ToolOutcome(content=f"Unknown tool: {call.name}", is_error=True)

    def _publish(self, event: dict[str, Any]) -> None:
        # The plugin's own event dicts (prompt / tool_call / approval_needed / done ...) are
        # superseded by the harness turn stream, which already carries each of them in its own
        # vocabulary. Dropping them also keeps live_output (a second Workflow Stream) unneeded.
        return None

    # ---- the two execution paths ----

    async def _run_harness_tool(
        self,
        runner: AgentWorkflowRunner,
        tool: Callable[..., Awaitable[Any]],
        call: DeferredCall,
        record: dict[str, Any],
    ) -> ToolOutcome:
        """A tool the harness defined: ``run_tool`` applies the policy and publishes events."""
        try:
            result = await runner.run_tool(call.id, tool, **call.input)
        except ToolApprovalDenied:
            record["status"] = "rejected"
            return ToolOutcome(content=_DENIED, is_error=True)
        except ApplicationError as err:
            record["status"] = "failed"
            return ToolOutcome(content=f"Tool failed: {err.message}", is_error=True)
        except Exception as err:  # noqa: BLE001 - cancellation is BaseException and passes
            record["status"] = "failed"
            return ToolOutcome(content=f"Tool failed: {err}", is_error=True)
        record["status"] = "done"
        return ToolOutcome(content=_stringify(result))

    async def _run_engine_tool(
        self,
        runner: AgentWorkflowRunner,
        call: DeferredCall,
        record: dict[str, Any],
    ) -> ToolOutcome:
        """A Claude Code tool the plugin runs as its own Activity (Bash, ``mcp__*``).

        The plugin's activity does the work; the harness adds what an MCP call gets from
        :func:`as_harness_mcp_server`: the policy gate, then ``tool_start`` and
        ``tool_end`` / ``tool_error`` under the model's own ``tool_use_id``.
        """
        safe = any(fnmatch.fnmatchcase(call.name, p) for p in self._safe_builtins)

        async def invoke() -> ToolOutcome:
            try:
                await _apply_approval_policy(call.name, call.input, inherently_safe=safe)
            except ToolApprovalDenied:
                record["status"] = "rejected"
                return ToolOutcome(content=_DENIED, is_error=True)
            runner.publish(
                ToolStartEvent(tool_id=call.id, tool_name=call.name, tool_input=call.input)
            )
            # super(): the plugin's path for engine calls. Its approval branch is dormant
            # (tool_approvals is empty), so this goes straight to the tool-step Activity.
            try:
                outcome = await super(HarnessClaudeAgent, self)._execute_tool(call, record)
            except BaseException as exc:
                # Close the bracket on cancellation or an unexpected failure too, so a consumer
                # is never left with a tool_start that never ends. Then let it travel on.
                runner.publish(
                    ToolErrorEvent(
                        tool_id=call.id,
                        tool_name=call.name,
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
                raise
            if outcome.is_error:
                runner.publish(
                    ToolErrorEvent(
                        tool_id=call.id,
                        tool_name=call.name,
                        message=_stringify(outcome.content),
                    )
                )
            else:
                runner.publish(
                    ToolEndEvent(
                        tool_id=call.id,
                        tool_name=call.name,
                        tool_output=_stringify(outcome.content),
                    )
                )
            return outcome

        return await runner.run_tool(call.id, invoke)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require_harness_tool(tool_callable: Callable[..., Any]) -> None:
    if any(getattr(tool_callable, attr, False) for attr in _HARNESS_TOOL_ATTRS):
        return
    name = getattr(tool_callable, "__name__", repr(tool_callable))
    raise TypeError(
        f"{name} is not a harness tool; decorate it with @agent.tool_defn or "
        "@agent.activity_tool_defn."
    )


def _input_schema(tool_callable: Callable[..., Any]) -> dict[str, Any]:
    """JSON Schema of the tool's MODEL-FACING parameters.

    The harness decorators stamp the callable with a ``__signature__`` / annotations from
    which ``self`` and ``Injected[...]`` parameters are already stripped, so reading it
    yields exactly what Claude should see.
    """
    fields: dict[str, Any] = {}
    try:
        # Resolves string annotations (``from __future__ import annotations``) the way pydantic
        # will need them.
        hints = get_type_hints(tool_callable, include_extras=True)
    except Exception:  # noqa: BLE001 - one unresolvable name must not break agent construction
        hints = {}
    raw = getattr(tool_callable, "__annotations__", {})
    namespace = getattr(inspect.unwrap(tool_callable), "__globals__", {})
    for pname, param in inspect.signature(tool_callable).parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        annotation = hints.get(pname)
        if annotation is None:
            # get_type_hints is all-or-nothing, so resolve this parameter on its own. One that
            # still cannot be resolved becomes an untyped parameter for the model, never an
            # error (pydantic would raise on a bare string).
            annotation = raw.get(pname, Any)
            for _ in range(3):  # a quoted annotation under ``from __future__`` is quoted twice
                if not isinstance(annotation, str):
                    break
                try:
                    annotation = eval(annotation, namespace)  # noqa: S307 - the tool author's own annotation
                except Exception:  # noqa: BLE001
                    annotation = Any
                    break
            if isinstance(annotation, (str, ForwardRef)):
                annotation = Any
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[pname] = (annotation, default)
    model = create_model(f"{tool_callable.__name__}_args", **fields)
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


def _stringify(result: Any) -> str:
    if isinstance(result, str):
        return result
    dump = getattr(result, "model_dump_json", None)
    if callable(dump):
        return str(dump())
    import json

    try:
        return json.dumps(result, default=str)
    except (TypeError, ValueError):
        return str(result)
