# ABOUTME: The subagent-toolset generator — turns a *statically chosen* harness agent into a
# set of tools a PARENT agent can use to drive it as a subagent. Given the child's class, it
# reads the child's ``@agent.accepts`` handlers statically (no workflow started) and emits, per
# wired agent ``key``:
#   * ``start_<key>()``         — start a child instance, return its short handle.
#   * ``<key>_<fn>(subagent, …)`` — one per handler; send a message to that instance + return
#                                   the reply, strongly typed to the handler's input/output.
#   * ``stop_<key>(subagent)``  — stop (close) a child instance.
#
# Each is an inline ``@agent.tool_defn`` callable, so it slots straight into a parent agent's
# existing tool-calling loop (``function_param(fn)`` reads its synthesized signature for the
# model schema; ``run_tool`` dispatches it) and inherits the harness's native approval gating +
# tool lifecycle events for free. The tools are stateless: at call time they resolve the live
# runner via ``_current_runner()`` (the ambient ``_CURRENT_RUNNER`` ``run_tool`` parks) and
# delegate to ``runner.start_subagent`` / ``run_subagent_turn`` / ``stop_subagent`` — so all
# subagent state lives on the runner, with no holder object or ``has_self`` plumbing.
#
# GUARDRAIL: what a parent model may drive is decided HERE, at toolset construction — not by
# a naming convention on the child, and not by what the child's ``agent_interface`` query
# admits to. A child handler's ``model_callable`` flag is only the author's HINT; the
# ``tools=`` :class:`SubagentToolPolicy` is authoritative and may honor it (the default),
# narrow it further, or ignore it entirely. Since a parent model can only ever act through
# the tools it was given, this is the real boundary, and it is the only one.
#
# Independently of any policy: the harness-owned updates are never generated as tools. A
# parent gets no approve-a-tool capability (``tool_approval``) and cannot fabricate a
# callback result (``provide_callback_result``), because neither is an ``@agent.accepts``
# handler — so a child's gated tools still escalate to a human, and its approval policy
# stays operator-owned even under ``dangerously_allow_all()``.

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from temporalio import workflow

from temporal_agent_harness.harness.agent_workflow import (
    _AcceptedHandler,
    _current_runner,
    agent_handlers,
    tool_defn,
)


@dataclass(frozen=True)
class SubagentToolPolicy:
    """The PARENT's decision about which of a child's handlers become model-callable tools.

    Mirrors the two-layer shape the harness already uses for tool approvals: the child
    declares a static hint (``@agent.accepts(model_callable=...)``, the analogue of a tool's
    ``inherently_safe``), and this policy — supplied by the parent as
    ``subagent_toolset(..., tools=...)`` — is authoritative. A hint is never an access
    decision; this is.

    Construct one with a named preset rather than by field:

      * :meth:`allow_model_callable` (the default) — honor the child's hints.
      * :meth:`allow_only` — a specific allow-list, ignoring the hints. Use it to hand a
        parent a deliberately narrow slice of a child that declares more.
      * :meth:`dangerously_allow_all` — every handler, hints ignored. Named as a yellow flag:
        it hands the parent's model whatever the child happens to expose, including handlers
        the child's author explicitly marked as not model-callable.

    No preset can reach ``tool_approval`` or ``provide_callback_result`` — those are not
    handlers, so the human-in-the-loop guardrail holds regardless of what is chosen here.
    """

    # None = "honor each handler's model_callable hint". A frozenset = that exact allow-list,
    # hints ignored. Empty frozenset is meaningful (allow nothing) and distinct from None.
    allowed_names: frozenset[str] | None = None
    # Include handlers whose hint says they are not model-callable.
    include_non_model_callable: bool = False

    @classmethod
    def allow_model_callable(cls) -> "SubagentToolPolicy":
        """Honor the child's hints: generate a tool for each ``model_callable=True`` handler."""
        return cls()

    @classmethod
    def allow_only(cls, *names: str) -> "SubagentToolPolicy":
        """Generate tools for exactly ``names``, whatever the child's hints say.

        Narrower OR wider than the hints — an explicitly named handler is included even if
        its author marked it ``model_callable=False``, because naming it here IS the parent
        overriding that hint on purpose."""
        return cls(allowed_names=frozenset(names), include_non_model_callable=True)

    @classmethod
    def dangerously_allow_all(cls) -> "SubagentToolPolicy":
        """Generate a tool for EVERY handler, ignoring the hints. See the class docstring."""
        return cls(include_non_model_callable=True)

    def selects(self, handler: _AcceptedHandler) -> bool:
        """Whether this policy admits ``handler`` as a generated tool."""
        if self.allowed_names is not None and handler.name not in self.allowed_names:
            return False
        return handler.model_callable or self.include_non_model_callable


def _resolve_workflow_type(agent_cls: type) -> str:
    """The child's registered ``@workflow.defn`` name (what ``start_child_workflow`` needs).

    Falls back to the class name when the class carries no ``@workflow.defn`` (or an unnamed
    one) — e.g. in unit tests that reflect over a bare ``@agent.accepts`` class."""
    defn = workflow._Definition.from_class(agent_cls)
    if defn is not None and defn.name:
        return defn.name
    return agent_cls.__name__


def _handler_param_name(handler: _AcceptedHandler) -> str:
    """The name of the handler's single input parameter (besides ``self``) — reused as the
    generated tool's input-object param name so the tool mirrors the agent's own contract."""
    return next(
        p for p in inspect.signature(handler.method).parameters if p != "self"
    )


def _make_start_tool(
    *, key: str, workflow_type: str, task_queue: str
) -> Callable[..., Awaitable[str]]:
    """Build the ``start_<key>`` tool: start a child instance and return its short handle."""

    async def _start() -> str:
        return await _current_runner().start_subagent(key, workflow_type, task_queue)

    _start.__name__ = f"start_{key}"
    _start.__qualname__ = _start.__name__
    _start.__doc__ = (
        f"Start a new {key} subagent and return its short handle. Call this first; then "
        f"pass the returned handle as the `subagent` argument to the {key}_* tools to drive "
        f"that instance, and to stop_{key} to shut it down. You may start several instances "
        f"to work on subtasks in parallel — each returns its own handle."
    )
    _start.__signature__ = inspect.Signature([], return_annotation=str)  # type: ignore[attr-defined]
    _start.__annotations__ = {"return": str}
    return tool_defn()(_start)


def _make_stop_tool(*, key: str) -> Callable[..., Awaitable[str]]:
    """Build the ``stop_<key>`` tool: close a child instance addressed by its handle."""

    async def _stop(subagent: str) -> str:
        await _current_runner().stop_subagent(subagent)
        return f"stopped subagent {subagent!r}"

    _stop.__name__ = f"stop_{key}"
    _stop.__qualname__ = _stop.__name__
    _stop.__doc__ = (
        f"Stop the {key} subagent identified by `subagent` (the handle returned by "
        f"start_{key}). Use it when that instance's work is done."
    )
    _stop.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                "subagent", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str
            )
        ],
        return_annotation=str,
    )
    _stop.__annotations__ = {"subagent": str, "return": str}
    return tool_defn()(_stop)


def _make_send_tool(
    *, key: str, handler: _AcceptedHandler
) -> Callable[..., Awaitable[BaseModel]]:
    """Build a ``<key>_<fn>`` tool for one of the child's ``@agent.accepts`` handlers.

    The synthesized signature is ``(subagent: str, <param>: InputModel) -> OutputModel`` using
    the handler's REAL input/output pydantic models, so the harness's ``function_param`` emits the
    correct nested object schema (field names + types + required) and the function is strongly
    typed end-to-end. (Gemini's ``from_callable`` drops nested per-field descriptions; the
    handler docstring still becomes the tool description.) At call time the model-supplied input
    arrives as a dict; the tool validates it into the input model, drives one subagent turn via
    the runner, and re-validates the reply dict into the output model (boundary validation)."""
    fn_name = handler.name
    input_type = handler.input_type
    output_type = handler.output_type
    param_name = _handler_param_name(handler)

    async def _send(subagent: str, **model_kwargs: Any) -> BaseModel:
        # The model passes the input under ``param_name`` as a raw dict; coerce + validate.
        payload = input_type.model_validate(model_kwargs.get(param_name, {}))
        output = await _current_runner().run_subagent_turn(
            subagent, fn_name, payload.model_dump(mode="json")
        )
        return output_type.model_validate(output)

    _send.__name__ = f"{key}_{fn_name}"
    _send.__qualname__ = _send.__name__
    _send.__doc__ = (
        f"{handler.description}\n\n"
        f"Sends this to the {key} subagent identified by `subagent` (the handle returned by "
        f"start_{key}) and returns its reply. Send several messages to the same handle to have "
        f"that instance expand on or clarify its earlier work."
    )
    _send.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                "subagent", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str
            ),
            inspect.Parameter(
                param_name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=input_type,
            ),
        ],
        return_annotation=output_type,
    )
    _send.__annotations__ = {
        "subagent": str,
        param_name: input_type,
        "return": output_type,
    }
    return tool_defn()(_send)


def subagent_toolset(
    agent_cls: type,
    *,
    key: str,
    task_queue: str,
    workflow_type: str | None = None,
    tools: SubagentToolPolicy | None = None,
) -> list[Callable[..., Awaitable[Any]]]:
    """Convert a harness agent into a toolset a parent agent can use to drive it as a subagent.

    Reads ``agent_cls``'s ``@agent.accepts`` handlers **statically** (no workflow started — pure
    reflection via :func:`agent_handlers`) and returns inline ``tool_defn`` callables:
    ``start_<key>``, one ``<key>_<fn>`` per handler, and ``stop_<key>``. Fold the returned list
    into the parent agent's tool set exactly like any other tools (declare each via
    ``function_param`` and dispatch via ``runner.run_tool``); they reach the runner — and its
    subagent registry — through the ambient ``_CURRENT_RUNNER`` at call time.

    Args:
        agent_cls: the child agent's ``@workflow.defn`` + ``@agent.defn`` class.
        key: short, stable namespace for this wired agent (a parent may wire several). Tool
            names are ``start_<key>`` / ``<key>_<fn>`` / ``stop_<key>``.
        task_queue: the task queue the child agent's worker polls (where instances are started).
        workflow_type: the child's registered workflow type name; defaults to its ``@workflow.defn``
            name.
        tools: the parent's authoritative :class:`SubagentToolPolicy` — which of the child's
            handlers become tools. Defaults to honoring the child's ``model_callable`` hints.
    """
    resolved_type = workflow_type or _resolve_workflow_type(agent_cls)
    policy = tools if tools is not None else SubagentToolPolicy.allow_model_callable()
    declared = agent_handlers(agent_cls)
    handlers = {
        name: handler for name, handler in declared.items() if policy.selects(handler)
    }
    if not handlers:
        raise TypeError(
            f"{agent_cls.__name__} exposes no handlers under the given SubagentToolPolicy, "
            f"so it has no callable surface to wire as a subagent toolset. It declares "
            f"{sorted(declared)}; of those, model-callable: "
            f"{sorted(n for n, h in declared.items() if h.model_callable)}. Widen the "
            f"policy (e.g. SubagentToolPolicy.allow_only(...)) or mark a handler "
            f"model_callable=True."
        )

    tools: list[Callable[..., Awaitable[Any]]] = [
        _make_start_tool(key=key, workflow_type=resolved_type, task_queue=task_queue)
    ]
    tools.extend(
        _make_send_tool(key=key, handler=handler) for handler in handlers.values()
    )
    tools.append(_make_stop_tool(key=key))
    return tools
