"""First-class harness support for the OpenAI Agents SDK.

The Temporal Python SDK already ships the low-level OpenAI Agents integration:
``temporalio.contrib.openai_agents``. This package makes that integration available
from the harness namespace and adds :func:`as_openai_agent_tool`, an adapter that
lets OpenAI Agents call harness tools while preserving the harness approval policy
and tool lifecycle event stream.

Install the optional dependencies with::

    uv sync --extra openai-agents

Worker/client setup uses the Temporal contrib plugin, re-exported lazily here::

    from temporal_agent_harness.ai_sdks.openai_agents_plugin import (
        ModelActivityParameters,
        OpenAIAgentsPlugin,
        OpenAIPayloadConverter,
    )
    from temporalio.converter import DataConverter

    temporal_client = await Client.connect(
        "localhost:7233",
        data_converter=DataConverter(payload_converter_class=OpenAIPayloadConverter),
        plugins=[
            OpenAIAgentsPlugin(
                model_params=ModelActivityParameters(
                    start_to_close_timeout=timedelta(seconds=60),
                ),
            ),
        ],
    )

Workflow code can then use the OpenAI Agents SDK normally and adapt harness tools::

    from agents import Agent, Runner
    from temporal_agent_harness.ai_sdks.openai_agents_plugin import as_openai_agent_tool

    sdk_agent = Agent(
        name="Assistant",
        instructions="Use the available tools when helpful.",
        tools=[as_openai_agent_tool(self._runner, search_docs)],
    )
    result = await Runner.run(sdk_agent, input=message.text)
"""

from __future__ import annotations

import importlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from temporalio import workflow
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

if TYPE_CHECKING:
    from agents import Tool


_INSTALL_MESSAGE = (
    "OpenAI Agents SDK support requires the optional `openai-agents` extra. "
    "Install it with `uv sync --extra openai-agents` or "
    "`pip install 'temporal-agent-harness[openai-agents]'`."
)

_CONTRIB_EXPORTS = {
    "AgentsWorkflowError": "temporalio.contrib.openai_agents",
    "ModelActivityParameters": "temporalio.contrib.openai_agents",
    "OpenAIAgentsPlugin": "temporalio.contrib.openai_agents",
    "OpenAIPayloadConverter": "temporalio.contrib.openai_agents",
    "SandboxClientProvider": "temporalio.contrib.openai_agents",
    "StatefulMCPServerProvider": "temporalio.contrib.openai_agents",
    "StatelessMCPServerProvider": "temporalio.contrib.openai_agents",
    "activity_as_tool": "temporalio.contrib.openai_agents.workflow",
    "nexus_operation_as_tool": "temporalio.contrib.openai_agents.workflow",
    "stateful_mcp_server": "temporalio.contrib.openai_agents.workflow",
    "stateless_mcp_server": "temporalio.contrib.openai_agents.workflow",
    "temporal_sandbox_client": "temporalio.contrib.openai_agents.workflow",
}

_HARNESS_TOOL_ATTRS = ("__agent_tool__", "__agent_activity_tool__")


def __getattr__(name: str) -> Any:
    """Lazily re-export Temporal's OpenAI Agents contrib symbols.

    The base harness can be installed without the OpenAI Agents SDK. Keeping these
    imports lazy means ``import temporal_agent_harness`` stays lightweight and the
    error appears only when the integration is actually used.
    """
    module_name = _CONTRIB_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    module = _import_optional(module_name)
    return getattr(module, name)


def as_openai_agent_tool(
    runner: AgentWorkflowRunner,
    tool_callable: Callable[..., Awaitable[Any]],
    *,
    injections: Mapping[str, Any] | None = None,
    strict_json_schema: bool = True,
) -> "Tool":
    """Adapt a harness tool into an OpenAI Agents SDK ``FunctionTool``.

    ``tool_callable`` must be produced by :func:`harness.agent.tool_defn`,
    :func:`harness.agent.activity_tool_defn`, or :func:`harness.agent.subagent_toolset`.
    The returned OpenAI tool invokes ``runner.run_tool(...)`` for every model tool
    call, so the harness remains responsible for:

    - safe-by-default approval policy evaluation;
    - ``tool_start`` / ``tool_end`` / ``tool_error`` event publication;
    - injected parameters declared with ``Injected[...]``;
    - activity-backed execution for ``@agent.activity_tool_defn`` tools.

    Use ``temporalio.contrib.openai_agents.workflow.activity_as_tool`` directly
    for plain Temporal activities that do not need harness approval or harness
    tool lifecycle events.
    """
    _require_harness_tool(tool_callable)
    function_schema, function_tool_cls = _agents_tool_symbols()
    schema = function_schema(tool_callable)

    async def on_invoke_tool(_ctx: Any, input: str) -> str:
        try:
            json_data = json.loads(input or "{}")
        except Exception as exc:  # noqa: BLE001 - converted to workflow-visible error
            raise ApplicationError(
                f"Invalid JSON input for tool {schema.name}: {input}",
                type="InvalidToolInput",
                non_retryable=True,
            ) from exc
        if not isinstance(json_data, dict):
            raise ApplicationError(
                f"Tool {schema.name} expected a JSON object input, "
                f"got {type(json_data).__name__}.",
                type="InvalidToolInput",
                non_retryable=True,
            )

        try:
            parsed = schema.params_pydantic_model(**json_data)
            args, kwargs = schema.to_call_args(parsed)
        except Exception as exc:  # noqa: BLE001 - preserve a non-retryable model input error
            raise ApplicationError(
                f"Payload for tool {schema.name!r} does not match its schema: {exc}",
                type="InvalidToolInput",
                non_retryable=True,
            ) from exc

        tool_call_id = getattr(_ctx, "tool_call_id", None)
        if not isinstance(tool_call_id, str) or not tool_call_id:
            tool_call_id = _new_tool_call_id(schema.name)

        try:
            result = await runner.run_tool(
                tool_call_id,
                tool_callable,
                *args,
                injections=injections,
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - model-visible tool failure
            return f"Tool {schema.name!r} failed: {exc}"
        return _stringify_tool_result(result)

    return function_tool_cls(
        name=schema.name,
        description=schema.description or "",
        params_json_schema=schema.params_json_schema,
        on_invoke_tool=on_invoke_tool,
        strict_json_schema=strict_json_schema,
    )


def as_openai_agent_tools(
    runner: AgentWorkflowRunner,
    tool_callables: (
        Mapping[str, Callable[..., Awaitable[Any]]]
        | list[Callable[..., Awaitable[Any]]]
        | tuple[Callable[..., Awaitable[Any]], ...]
    ),
    *,
    injections: Mapping[str, Any] | None = None,
    strict_json_schema: bool = True,
) -> list["Tool"]:
    """Adapt several harness tools into OpenAI Agents SDK tools."""
    tools = (
        list(tool_callables.values())
        if isinstance(tool_callables, Mapping)
        else list(tool_callables)
    )
    return [
        as_openai_agent_tool(
            runner,
            tool,
            injections=injections,
            strict_json_schema=strict_json_schema,
        )
        for tool in tools
    ]


def _require_harness_tool(tool_callable: Callable[..., Any]) -> None:
    if any(getattr(tool_callable, attr, False) for attr in _HARNESS_TOOL_ATTRS):
        return
    name = getattr(tool_callable, "__name__", repr(tool_callable))
    raise TypeError(
        f"{name} is not a harness tool; decorate it with @agent.tool_defn, "
        "@agent.activity_tool_defn, or use agent.subagent_toolset(...)."
    )


def _agents_tool_symbols() -> tuple[Callable[[Callable[..., Any]], Any], type[Any]]:
    function_schema = getattr(
        _import_optional("agents.function_schema"), "function_schema"
    )
    function_tool_cls = getattr(_import_optional("agents.tool"), "FunctionTool")
    return function_schema, function_tool_cls


def _import_optional(module_name: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing == module_name or missing.split(".")[0] in {
            "agents",
            "openai",
            "mcp",
        }:
            raise RuntimeError(_INSTALL_MESSAGE) from exc
        raise


def _new_tool_call_id(tool_name: str) -> str:
    if _in_workflow():
        suffix = workflow.uuid4()
    else:
        suffix = uuid.uuid4()
    return f"openai_agents:{tool_name}:{suffix}"


def _in_workflow() -> bool:
    try:
        return workflow.in_workflow()
    except RuntimeError:
        return False


def _stringify_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, BaseModel):
        return result.model_dump_json()
    if isinstance(result, (dict, list, tuple, int, float, bool)) or result is None:
        return json.dumps(result, default=str)
    return str(result)


__all__ = [
    "AgentsWorkflowError",
    "ModelActivityParameters",
    "OpenAIAgentsPlugin",
    "OpenAIPayloadConverter",
    "SandboxClientProvider",
    "StatefulMCPServerProvider",
    "StatelessMCPServerProvider",
    "activity_as_tool",
    "as_openai_agent_tool",
    "as_openai_agent_tools",
    "nexus_operation_as_tool",
    "stateful_mcp_server",
    "stateless_mcp_server",
    "temporal_sandbox_client",
]
