# ABOUTME: Build start, send, and stop tools for a statically declared subagent.
# The tools use the active workflow runner and preserve normal tool approval and events.
# They do not expose operator-only approval or slash-command handlers.

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from pydantic import BaseModel
from temporalio import workflow

from temporal_agent_harness.harness.agent_protocol import SubagentTransport
from temporal_agent_harness.harness.agent_workflow import (
    _SLASH_MESSAGE_TYPE,
    _AcceptedHandler,
    _current_runner,
    agent_handlers,
    tool_defn,
)
from temporal_agent_harness.harness.subagent_nexus_transport import NexusTransport
from temporal_agent_harness.harness.subagent_transport import ChildWorkflowTransport


def _resolve_workflow_type(agent_cls: type) -> str:
    """Return the registered workflow name, or use the class name."""
    defn = workflow._Definition.from_class(agent_cls)
    if defn is not None and defn.name:
        return defn.name
    return agent_cls.__name__


def _handler_param_name(handler: _AcceptedHandler) -> str:
    """Return the handler input parameter name."""
    return next(p for p in inspect.signature(handler.method).parameters if p != "self")


def declared_handler(
    name: str,
    description: str,
    input_type: type[BaseModel],
    output_type: type[BaseModel],
    *,
    param_name: str = "input",
) -> _AcceptedHandler:
    """Declare a remote handler when no local agent class is available."""

    async def _stub(self: Any, **_: Any) -> BaseModel: ...

    _stub.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            inspect.Parameter(
                param_name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=input_type,
            ),
        ],
        return_annotation=output_type,
    )
    return _AcceptedHandler(
        name=name,
        input_type=input_type,
        output_type=output_type,
        description=description,
        method=_stub,
    )


def _make_start_tool(
    *, key: str, transport: SubagentTransport
) -> Callable[..., Awaitable[str]]:
    """Build a tool that starts a subagent."""

    async def _start() -> str:
        return await _current_runner().start_subagent(key, transport)

    _start.__name__ = f"start_{key}"
    _start.__qualname__ = _start.__name__
    _start.__doc__ = (
        f"Acquire a {key} subagent and return its short handle. The active subagent reuse "
        f"policy decides whether this returns an existing instance or starts a new one. "
        f"Call this first; then "
        f"pass the returned handle as the `subagent` argument to the {key}_* tools to drive "
        f"that instance, and to stop_{key} to shut it down. Set the policy to `always-new` "
        f"before acquiring several instances for parallel subtasks."
    )
    _start.__signature__ = inspect.Signature([], return_annotation=str)  # type: ignore[attr-defined]
    _start.__annotations__ = {"return": str}
    return tool_defn()(_start)


def _make_stop_tool(*, key: str) -> Callable[..., Awaitable[str]]:
    """Build a tool that stops a subagent."""

    async def _authorize_stop(tool_name: str, tool_input: dict[str, Any]) -> None:
        await _current_runner().authorize_subagent_stop(
            tool_name, str(tool_input["subagent"])
        )

    async def _stop(subagent: str) -> str:
        runner = _current_runner()
        if not await runner.request_subagent_stop(subagent):
            return f"kept subagent {subagent!r} open by lifecycle policy"
        return f"stopped subagent {subagent!r}"

    _stop.__name__ = f"stop_{key}"
    _stop.__qualname__ = _stop.__name__
    _stop.__doc__ = (
        f"Request that the {key} subagent identified by `subagent` be stopped. The "
        f"active subagent close policy decides whether it stays open, closes "
        f"immediately, or requires user approval."
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
    return tool_defn(_approval_gate=_authorize_stop)(_stop)


def _make_send_tool(
    *, key: str, handler: _AcceptedHandler
) -> Callable[..., Awaitable[BaseModel]]:
    """Build a typed tool for one subagent handler."""
    fn_name = handler.name
    input_type = handler.input_type
    output_type = handler.output_type
    param_name = _handler_param_name(handler)

    async def _send(subagent: str, **model_kwargs: Any) -> BaseModel:
        # Validate the model input and the subagent output.
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
            # ``_send`` accepts the model input through ``**model_kwargs``.
            inspect.Parameter(
                param_name,
                inspect.Parameter.KEYWORD_ONLY,
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
    agent_cls_or_handlers: type | Sequence[_AcceptedHandler],
    *,
    key: str,
    transport: SubagentTransport | None = None,
    task_queue: str | None = None,
    workflow_type: str | None = None,
) -> list[Callable[..., Awaitable[Any]]]:
    """Convert a subagent into a toolset a parent agent can use to drive it.

    The result contains ``start_<key>``, one ``<key>_<fn>`` per handler, and
    ``stop_<key>``.

    Args:
        agent_cls_or_handlers: A local agent class or declared remote handlers.
        key: The stable namespace used in each generated tool name.
        transport: The transport to the subagent. The default is a child workflow.
        task_queue: The child task queue for the default transport.
        workflow_type: The child workflow type for the default transport.
    """
    if transport is None:
        if not isinstance(agent_cls_or_handlers, type):
            raise TypeError(
                "subagent_toolset needs an explicit transport when agent_cls_or_handlers is "
                "a declared handler list, not a class — there is no local workflow to start."
            )
        if task_queue is None:
            raise TypeError(
                "subagent_toolset needs task_queue (to build the default "
                "ChildWorkflowTransport) or an explicit transport."
            )
        resolved_type = workflow_type or _resolve_workflow_type(agent_cls_or_handlers)
        transport = ChildWorkflowTransport(resolved_type, task_queue)

    if isinstance(agent_cls_or_handlers, type):
        handlers = {
            name: handler
            for name, handler in agent_handlers(agent_cls_or_handlers).items()
            if name != _SLASH_MESSAGE_TYPE
        }
        label = agent_cls_or_handlers.__name__
    else:
        handlers = {handler.name: handler for handler in agent_cls_or_handlers}
        label = key
    if not handlers:
        raise TypeError(
            f"{label} declares no callable handlers, so it has no callable surface to wire "
            f"as a subagent toolset."
        )

    tools: list[Callable[..., Awaitable[Any]]] = [
        _make_start_tool(key=key, transport=transport)
    ]
    tools.extend(
        _make_send_tool(key=key, handler=handler) for handler in handlers.values()
    )
    tools.append(_make_stop_tool(key=key))
    return tools


def nexus_native_subagent(
    agent_cls: type, endpoint: str, *, key: str
) -> list[Callable[..., Awaitable[Any]]]:
    """Build tools for a harness agent reached directly through Nexus.

    Example:
        tools = nexus_native_subagent(ResearchAgentWorkflow, "research-agent-endpoint", key="research")
    """
    return subagent_toolset(agent_cls, key=key, transport=NexusTransport(endpoint))


class SubagentGateway:
    """Access gateway subagents registered for one ``agent_id``."""

    def __init__(
        self,
        agent_id: str,
        gateway_name: str = "RegistryService",
        gateway_endpoint: str = "mcp-registry-endpoint",
    ) -> None:
        self._agent_id = agent_id
        self._gateway_name = gateway_name
        self._gateway_endpoint = gateway_endpoint

    def subagent(
        self,
        agent_cls_or_handlers: type | Sequence[_AcceptedHandler],
        alias: str,
        *,
        key: str,
    ) -> list[Callable[..., Awaitable[Any]]]:
        """Build tools for the subagent registered under ``alias``."""
        from temporal_agent_harness.harness.subagent_gateway_transport import (
            GatewayTransport,
        )

        transport = GatewayTransport(
            self._agent_id, alias, self._gateway_name, self._gateway_endpoint
        )
        return subagent_toolset(agent_cls_or_handlers, key=key, transport=transport)


def nexus_subagent_gateway(
    agent_id: str | None = None,
    *,
    gateway_name: str = "RegistryService",
    gateway_endpoint: str = "mcp-registry-endpoint",
) -> SubagentGateway:
    """Access registered subagents for an agent ID.

    The default agent ID is the current workflow type.

    Example:
        gateway = nexus_subagent_gateway()
        tools = gateway.subagent([declared_handler(...)], "writer", key="writer")
    """
    return SubagentGateway(
        agent_id or workflow.info().workflow_type, gateway_name, gateway_endpoint
    )
