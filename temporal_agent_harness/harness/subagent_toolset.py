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
# GUARDRAIL: generated toolsets omit operator-only channels. A parent model gets no
# approve-a-tool capability (``tool_approval``) and no slash-command runtime controls
# (``slash``), so a child's gated tools still escalate to a human and approval policy stays
# operator-owned.

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel
from temporalio import workflow

from temporal_agent_harness.harness.agent_workflow import (
    _AcceptedHandler,
    _SLASH_MESSAGE_TYPE,
    _current_runner,
    agent_handlers,
    tool_defn,
)


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
    typed end-to-end. At call time the model-supplied input arrives as a dict; the tool validates
    it into the input model, drives one subagent turn via the runner, and re-validates the reply
    dict into the output model (boundary validation)."""
    fn_name = handler.name
    input_type = handler.input_type
    output_type = handler.output_type
    param_name = _handler_param_name(handler)

    async def _send(
        subagent: str, *model_args: Any, **model_kwargs: Any
    ) -> BaseModel:
        # Schema adapters may dispatch the synthesized ``(subagent, <param>)`` signature
        # positionally or by keyword. Normalize both shapes before validating.
        if len(model_args) > 1:
            raise TypeError(
                f"{key}_{fn_name} expected at most one positional payload argument, "
                f"got {len(model_args)}"
            )
        if model_args and param_name in model_kwargs:
            raise TypeError(
                f"{key}_{fn_name} got multiple values for argument {param_name!r}"
            )
        if model_args:
            raw_payload = model_args[0]
        else:
            raw_payload = model_kwargs.pop(param_name, {})
        if model_kwargs:
            unexpected = ", ".join(sorted(model_kwargs))
            raise TypeError(f"{key}_{fn_name} got unexpected argument(s): {unexpected}")

        payload = input_type.model_validate(raw_payload)
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
    """
    resolved_type = workflow_type or _resolve_workflow_type(agent_cls)
    handlers = {
        name: handler
        for name, handler in agent_handlers(agent_cls).items()
        if name != _SLASH_MESSAGE_TYPE
    }
    if not handlers:
        raise TypeError(
            f"{agent_cls.__name__} declares no @agent.accepts handlers that are "
            f"model-callable, so it has no callable surface to wire as a subagent toolset."
        )

    tools: list[Callable[..., Awaitable[Any]]] = [
        _make_start_tool(key=key, workflow_type=resolved_type, task_queue=task_queue)
    ]
    tools.extend(
        _make_send_tool(key=key, handler=handler) for handler in handlers.values()
    )
    tools.append(_make_stop_tool(key=key))
    return tools
