# ABOUTME: Harness-owned glue for the vendored Strands Agents integration. Holds everything
# harness-specific that the vendored `strands_agents/` package does not know about: the live
# translator (`StrandsStreamObserver`) that folds raw Strands stream events into the harness
# turn-stream vocabulary, the observer factory + `harness_stream_route` that route those events to
# the in-flight turn, `as_strands_tool(s)` adapting harness tools onto Strands, the governance that
# puts `TemporalMCPClient` and `TemporalSandbox` tools under the harness approval policy and tool
# lifecycle, and `harness_strands_agent`, which builds a `TemporalAgent` wired to all of it.

"""Harness integration for the Strands Agents SDK.

Build the vendored plugin with the harness observer on the worker:

>>> from temporal_agent_harness.ai_sdks.strands_agents import StrandsPlugin
>>> from temporal_agent_harness.ai_sdks.strands_agents_harness import harness_observer_factory
>>> plugin = StrandsPlugin(
...     models={"claude": lambda: AnthropicModel(...)},
...     observer_factory=harness_observer_factory,
... )

and build the agent in workflow code with :func:`harness_strands_agent`, passing the agent's
runner, harness tools, ``TemporalMCPClient`` handles, and optionally a ``TemporalSandbox``. Its model
calls then stream ``reply_delta`` / ``thought_summary`` / ``tool_requested`` and
``model_interaction_started`` / ``…_ended`` onto the turn stream live, and every tool call runs
through ``runner.run_tool`` with the agent's approval policy, ``tool_start`` / ``tool_end`` /
``tool_error`` and steering.

.. warning::
    This integration is experimental and may change in future versions.
"""

from __future__ import annotations

import inspect
import json
import keyword
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Self

from pydantic import BaseModel
from strands.hooks import BeforeToolCallEvent
from strands.tools.decorator import FunctionToolMetadata
from strands.tools.mcp import MCPAgentTool
from strands.types._events import ToolInterruptEvent
from strands.types.agent import AgentInput
from strands.types.events import ToolResultEvent
from strands.types.streaming import StreamEvent
from strands.types.tools import (
    AgentTool,
    ToolGenerator,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
    ToolUse,
)

from temporalio.exceptions import ApplicationError

from temporal_agent_harness.ai_sdks.integration_helpers import StreamObserver
from temporal_agent_harness.ai_sdks.strands_agents import (
    TemporalAgent,
    TemporalMCPClient,
    TemporalSandbox,
)
from temporal_agent_harness.harness.agent_protocol import (
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    ThoughtSummaryDelta,
    TokenUsage,
    ToolRequested,
)
from temporal_agent_harness.harness.agent_workflow import (
    AgentWorkflowRunner,
    TurnEventPublisher,
    tool_defn,
)
from temporal_agent_harness.harness.stream_context import TurnStreamContext

__all__ = [
    "StrandsStreamObserver",
    "harness_observer_factory",
    "harness_stream_route",
    "as_strands_tool",
    "as_strands_tools",
    "harness_strands_agent",
    "STRANDS_INTERRUPT_NOT_SUPPORTED",
]

_HARNESS_TOOL_ATTRS = ("__agent_tool__", "__agent_activity_tool__")

STRANDS_INTERRUPT_NOT_SUPPORTED = "StrandsInterruptNotSupported"
_INTERRUPT_REFUSAL = (
    "Strands interrupts are not supported in a harness Strands agent. To get input from a "
    "person or a client, declare an @agent.callback_tool_defn tool and pass it through "
    "as_strands_tool(runner, ...); its call waits in the workflow for the result. To gate "
    "tool calls on a person's approval, use the agent's tool approval policy."
)


# ---------------------------------------------------------------------------
# Live streaming: raw Strands stream events -> harness turn vocabulary
# ---------------------------------------------------------------------------


class _HarnessStreamToken(BaseModel):
    """The routing token the harness hands the streaming model activity.

    The in-flight turn's :class:`TurnStreamContext` and the model id, so the observer can
    open the model-interaction bracket, naming the model, at dispatch. It rides the activity
    input as an untyped field and arrives in the activity as a plain dict.
    """

    context: TurnStreamContext
    model: str | None = None


def harness_stream_route(
    runner: AgentWorkflowRunner,
) -> Callable[[str | None], _HarnessStreamToken | None]:
    """The ``stream_route`` that sends an agent's model calls to ``runner``'s current turn.

    ``TemporalModel`` calls the route in workflow code before each model call. It returns
    ``None`` outside a turn, and the call then runs without live streaming.
    """

    def route(model: str | None) -> _HarnessStreamToken | None:
        context = runner.current_stream_context
        if context is None:
            return None
        return _HarnessStreamToken(context=context, model=model)

    return route


def harness_observer_factory(
    token: Any, *, batch_interval: timedelta | None = None
) -> StreamObserver[Any]:
    """Turn a streamed call's routing token into a fresh :class:`StrandsStreamObserver`.

    Wired onto the plugin as ``StrandsPlugin(observer_factory=...)``.
    """
    tok = (
        token
        if isinstance(token, _HarnessStreamToken)
        else _HarnessStreamToken.model_validate(token)
    )
    return StrandsStreamObserver(tok.context, model=tok.model, batch_interval=batch_interval)


class StrandsStreamObserver:
    """Translate one streamed Strands model call into harness turn events, live.

    One instance per call, so its per-call state (the open tool-use block, captured usage)
    never mixes across concurrent calls on a shared worker. ``__aenter__`` opens a
    :class:`TurnEventPublisher` for the turn and publishes ``model_interaction_started`` at
    dispatch, before the first event; ``__aexit__`` always publishes ``…_ended`` with the
    usage from the stream's ``metadata`` event.

    A tool call streams as ``contentBlockStart`` (with ``toolUseId`` and name), input
    fragments in ``contentBlockDelta.delta.toolUse.input``, and ``contentBlockStop``; it is
    published as one ``tool_requested`` keyed by ``toolUseId``, the id the tool wrappers
    hand to ``run_tool``, so ``tool_requested`` and ``tool_start`` share a ``tool_id``.
    """

    def __init__(
        self,
        context: TurnStreamContext,
        *,
        model: str | None = None,
        batch_interval: timedelta | None = None,
    ) -> None:
        self._context = context
        self._model = model
        self._batch_interval = batch_interval
        self._stack: AsyncExitStack | None = None
        self._publisher: TurnEventPublisher | None = None
        # The open tool-use block: (toolUseId, name, accumulated input fragments).
        self._tool_use: tuple[str, str, str] | None = None
        self._usage: TokenUsage | None = None

    async def __aenter__(self) -> Self:
        self._stack = AsyncExitStack()
        self._publisher = await self._stack.enter_async_context(
            AgentWorkflowRunner.publisher_from_activity(
                self._context, batch_interval=self._batch_interval
            )
        )
        self._publisher.publish(ModelInteractionStarted(model=self._model))
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        try:
            if self._publisher is not None:
                self._publisher.publish(
                    ModelInteractionEnded(model=self._model, usage=self._usage)
                )
        finally:
            if self._stack is not None:
                await self._stack.aclose()
                self._stack = None
            self._publisher = None
        return False

    async def on_event(self, event: StreamEvent) -> None:
        """Fold one raw Strands stream event into harness vocabulary."""
        pub = self._publisher
        if pub is None:
            return
        if "contentBlockStart" in event:
            tool_use = event["contentBlockStart"].get("start", {}).get("toolUse")
            if tool_use is not None:
                self._tool_use = (tool_use["toolUseId"], tool_use["name"], "")
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            if text := delta.get("text"):
                pub.publish(ReplyDelta(text=text))
            if (reasoning := delta.get("reasoningContent")) is not None:
                thought = {
                    key: value
                    for key, value in reasoning.items()
                    if key in ("text", "signature") and value
                }
                if thought:
                    pub.publish(ThoughtSummaryDelta(delta=thought))
            if (tool_input := delta.get("toolUse")) is not None and self._tool_use:
                tool_id, name, buffered = self._tool_use
                self._tool_use = (tool_id, name, buffered + tool_input.get("input", ""))
        elif "contentBlockStop" in event:
            if self._tool_use is not None:
                tool_id, name, buffered = self._tool_use
                self._tool_use = None
                pub.publish(
                    ToolRequested(
                        tool_id=tool_id, tool_name=name, tool_input=_parse_tool_input(buffered)
                    )
                )
        elif "metadata" in event:
            usage = event["metadata"].get("usage")
            if usage is not None:
                self._usage = TokenUsage(
                    input_tokens=usage.get("inputTokens"),
                    output_tokens=usage.get("outputTokens"),
                    total_tokens=usage.get("totalTokens"),
                    cached_tokens=usage.get("cacheReadInputTokens"),
                )


def _parse_tool_input(raw: str) -> dict[str, Any]:
    """Best-effort parse of a tool call's streamed JSON input, for display and approval."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Tool adapter: harness tools -> Strands AgentTools
# ---------------------------------------------------------------------------


class _HarnessStrandsTool(AgentTool):
    """A harness tool presented to Strands; every call runs through ``runner.run_tool``."""

    def __init__(
        self,
        runner: AgentWorkflowRunner,
        tool_callable: Callable[..., Awaitable[Any]],
        injections: Mapping[str, Any] | None,
    ) -> None:
        super().__init__()
        _require_harness_tool(tool_callable)
        self._runner = runner
        self._tool_callable = tool_callable
        self._injections = injections
        # The callable's model-facing __signature__ hides Injected parameters from the spec.
        self._metadata = FunctionToolMetadata(tool_callable)
        self._spec: ToolSpec = self._metadata.extract_metadata()

    @property
    def tool_name(self) -> str:
        return self._spec["name"]

    @property
    def tool_spec(self) -> ToolSpec:
        return self._spec

    @property
    def tool_type(self) -> str:
        return "harness"

    async def stream(
        self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any
    ) -> ToolGenerator:
        tool_id = tool_use["toolUseId"]
        try:
            arguments = self._metadata.validate_input(tool_use.get("input") or {})
            result = await self._runner.run_tool(
                tool_id, self._tool_callable, injections=self._injections, **arguments
            )
        except Exception as exc:  # noqa: BLE001 - a model-visible tool failure
            text = f"Tool {self.tool_name!r} failed: {exc}"
            yield ToolResultEvent(
                _tool_result(tool_id, "error", _with_steering(self._runner, tool_id, text)),
                exc,
            )
            return
        yield ToolResultEvent(
            _tool_result(
                tool_id, "success", _with_steering(self._runner, tool_id, _stringify(result))
            )
        )


def as_strands_tool(
    runner: AgentWorkflowRunner,
    tool_callable: Callable[..., Awaitable[Any]],
    *,
    injections: Mapping[str, Any] | None = None,
) -> AgentTool:
    """Adapt a harness tool into a Strands ``AgentTool``.

    ``tool_callable`` must be produced by :func:`harness.agent.tool_defn`,
    :func:`harness.agent.activity_tool_defn`, or :func:`harness.agent.subagent_toolset`.
    Each call runs ``runner.run_tool(...)`` under the call's ``toolUseId``, so the harness
    applies the approval policy, publishes ``tool_start`` / ``tool_end`` / ``tool_error``,
    fills ``Injected[...]`` parameters, and appends any steering to the result.
    """
    return _HarnessStrandsTool(runner, tool_callable, injections)


def as_strands_tools(
    runner: AgentWorkflowRunner,
    tool_callables: (
        Mapping[str, Callable[..., Awaitable[Any]]] | Iterable[Callable[..., Awaitable[Any]]]
    ),
    *,
    injections: Mapping[str, Any] | None = None,
) -> list[AgentTool]:
    """Adapt several harness tools into Strands ``AgentTool`` s."""
    tools = (
        list(tool_callables.values())
        if isinstance(tool_callables, Mapping)
        else list(tool_callables)
    )
    return [as_strands_tool(runner, tool, injections=injections) for tool in tools]


def _require_harness_tool(tool_callable: Callable[..., Any]) -> None:
    if any(getattr(tool_callable, attr, False) for attr in _HARNESS_TOOL_ATTRS):
        return
    name = getattr(tool_callable, "__name__", repr(tool_callable))
    raise TypeError(
        f"{name} is not a harness tool; decorate it with @agent.tool_defn, "
        "@agent.activity_tool_defn, @agent.callback_tool_defn, or use "
        "agent.subagent_toolset(...)."
    )


# ---------------------------------------------------------------------------
# Governance: MCP and sandbox tools run as harness tools
# ---------------------------------------------------------------------------


class _GovernedStrandsTool(AgentTool):
    """An MCP or sandbox Strands tool whose every call runs as an ``@agent.tool_defn`` tool.

    Each call wraps the inner tool in a ``tool_defn`` named and described as it is, and runs
    that through ``runner.run_tool`` under the call's ``toolUseId``. ``tool_defn`` applies the
    approval policy and publishes ``tool_start`` and ``tool_end`` or ``tool_error``, exactly
    as for a harness tool. The model gets the inner tool's own result, images included, with
    any steering appended; a denied or failed call reaches it as an error result.
    """

    def __init__(self, runner: AgentWorkflowRunner, tool: AgentTool) -> None:
        super().__init__()
        self._runner = runner
        self._tool = tool

    @property
    def tool_name(self) -> str:
        return self._tool.tool_name

    @property
    def tool_spec(self) -> ToolSpec:
        return self._tool.tool_spec

    @property
    def tool_type(self) -> str:
        return self._tool.tool_type

    @property
    def supports_hot_reload(self) -> bool:
        return self._tool.supports_hot_reload

    async def stream(
        self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any
    ) -> ToolGenerator:
        tool_id = tool_use["toolUseId"]
        raw_input = tool_use.get("input")
        tool_input = dict(raw_input) if isinstance(raw_input, Mapping) else {}
        outcome: list[ToolResult] = []
        interrupts: list[ToolInterruptEvent] = []

        async def call(**_arguments: Any) -> str:
            # The inner tool reads its arguments from tool_use; the keyword arguments only
            # give tool_defn the call's input for the approval gate and tool_start.
            result: ToolResult | None = None
            async for event in self._tool.stream(tool_use, invocation_state, **kwargs):
                if isinstance(event, ToolInterruptEvent):
                    interrupts.append(event)
                    raise RuntimeError(_INTERRUPT_REFUSAL)
                if isinstance(event, ToolResultEvent):
                    result = event.tool_result
            if result is None:
                raise RuntimeError("The tool returned no result.")
            outcome.append(result)
            text = _result_text(result)
            if result["status"] == "error":
                raise RuntimeError(text)
            return text

        call.__name__ = call.__qualname__ = self.tool_name
        call.__doc__ = self.tool_spec.get("description")
        call.__signature__ = _input_signature(tool_input)  # type: ignore[attr-defined]
        harness_tool = tool_defn()(call)

        try:
            await self._runner.run_tool(tool_id, harness_tool, **tool_input)
        except Exception as exc:  # noqa: BLE001 - a model-visible tool failure
            if interrupts:
                # Passed on, the interrupt ends the run, where _HarnessStrandsAgent refuses it.
                yield interrupts[0]
                return
            result = _failure_result(tool_id, self.tool_name, exc, outcome)
        else:
            result = outcome[0]
        steering = self._runner.claim_steering(tool_id=tool_id)
        if steering is not None:
            result = ToolResult(
                toolUseId=result["toolUseId"],
                status=result["status"],
                content=[*result["content"], {"text": steering.render()}],
            )
        yield ToolResultEvent(result)


def _input_signature(tool_input: Mapping[str, Any]) -> inspect.Signature:
    """A signature whose parameters are the call's argument names, so ``tool_defn`` reports
    the input as the model sent it. Names that are not Python identifiers fall back to one
    ``arguments`` parameter holding them all."""
    if all(name.isidentifier() and not keyword.iskeyword(name) for name in tool_input):
        return inspect.Signature(
            [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY) for name in tool_input]
        )
    return inspect.Signature([inspect.Parameter("arguments", inspect.Parameter.VAR_KEYWORD)])


def _failure_result(
    tool_id: str, name: str, exc: Exception, outcome: list[ToolResult]
) -> ToolResult:
    """The inner tool's own error result, or one describing why the call did not run."""
    if outcome:
        return outcome[0]
    return _tool_result(tool_id, "error", f"Tool {name!r} failed: {exc}")


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class _HarnessStrandsAgent(TemporalAgent):
    """A ``TemporalAgent`` that refuses Strands interrupts.

    A tool or a hook that interrupts ends the run with ``stop_reason="interrupt"``, waiting
    to be resumed with answers. Under the harness, callback tools and the approval policy
    take that role, so such a run fails with a non-retryable ``ApplicationError``.
    ``invoke_async`` drains ``stream_async``, so this covers both.
    """

    async def stream_async(
        self, prompt: AgentInput = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        async for event in super().stream_async(prompt, **kwargs):
            result = event.get("result") if isinstance(event, Mapping) else None
            if result is not None and getattr(result, "stop_reason", None) == "interrupt":
                raise ApplicationError(
                    _INTERRUPT_REFUSAL, type=STRANDS_INTERRUPT_NOT_SUPPORTED, non_retryable=True
                )
            yield event


def harness_strands_agent(
    runner: AgentWorkflowRunner,
    *,
    tools: Iterable[AgentTool | TemporalMCPClient] = (),
    sandbox: TemporalSandbox | None = None,
    **agent_kwargs: Any,
) -> TemporalAgent:
    """Build a ``TemporalAgent`` that streams onto ``runner``'s turns and is fully governed.

    ``tools`` takes harness tools adapted with :func:`as_strands_tool` (from
    ``@agent.tool_defn``, ``@agent.activity_tool_defn`` or ``@agent.callback_tool_defn``) and
    ``TemporalMCPClient`` handles to MCP servers registered on the worker with
    ``StrandsPlugin(mcp_clients=...)``; anything else is refused. Each MCP tool call runs as
    an activity, under the agent's approval policy with ``tool_start`` / ``tool_end`` and
    steering, like a harness tool. ``sandbox`` is optional and must be a ``TemporalSandbox``: its
    operations run as activities against the worker's ``StrandsPlugin(sandboxes=...)``
    factory, and the tools it vends (``sandbox_shell``, ``sandbox_file_editor``) run under the
    agent's approval policy with ``tool_start`` / ``tool_end`` and steering, like harness tools.

    Every other keyword argument is passed to ``TemporalAgent`` (``model``, activity options,
    ``hooks``, ``system_prompt``, …), except ``streaming_topic`` and ``stream_route``: a
    harness agent always streams onto its turn. Strands interrupts are refused (see
    ``_HarnessStrandsAgent``): get input from a person or client with an
    ``@agent.callback_tool_defn`` tool.
    """
    for key in ("streaming_topic", "stream_route"):
        if key in agent_kwargs:
            raise ValueError(
                f"harness_strands_agent sets the agent's streaming itself; remove {key}=..."
            )
    tool_list: list[AgentTool | TemporalMCPClient] = list(tools)
    for tool in tool_list:
        if not isinstance(tool, (_HarnessStrandsTool, TemporalMCPClient)):
            raise TypeError(
                f"{_tool_label(tool)} is not a harness tool; a harness Strands agent takes "
                "only @agent.tool_defn, @agent.activity_tool_defn and @agent.callback_tool_defn "
                "tools adapted with as_strands_tool(runner, ...), TemporalMCPClient handles, "
                "and a TemporalSandbox's tools through sandbox=."
            )
    if sandbox is not None:
        if not isinstance(sandbox, TemporalSandbox):
            raise TypeError(
                f"sandbox must be a TemporalSandbox, not {type(sandbox).__name__}; register the "
                "real sandbox on the worker with StrandsPlugin(sandboxes={name: factory}) and "
                "pass TemporalSandbox(name)."
            )
        # Strands registers the sandbox's own tools at construction unless a tool with the same
        # name is already registered, so registering the governed ones first replaces them.
        tool_list += [
            _GovernedStrandsTool(runner, sandbox_tool)
            for sandbox_tool in sandbox.get_tools()
        ]
        agent_kwargs["sandbox"] = sandbox
    agent = _HarnessStrandsAgent(
        stream_route=harness_stream_route(runner), tools=tool_list, **agent_kwargs
    )

    def govern_mcp_call(event: BeforeToolCallEvent) -> None:
        # MCP tools are listed before each model call, after this function returns, so each
        # call is governed as it starts. Registered last, this hook sees the tool any other
        # hook selected.
        tool = event.selected_tool
        if tool is not None and tool.tool_name != event.tool_use["name"]:
            # Another hook renamed the call; Strands runs the registry's tool for the new name.
            tool = event.agent.tool_registry.registry.get(event.tool_use["name"], tool)
        if isinstance(tool, MCPAgentTool):
            event.selected_tool = _GovernedStrandsTool(runner, tool)

    agent.hooks.add_callback(BeforeToolCallEvent, govern_mcp_call)
    ungoverned = [
        name
        for name, tool in agent.tool_registry.registry.items()
        if not isinstance(tool, (_HarnessStrandsTool, _GovernedStrandsTool))
    ]
    if ungoverned:
        raise TypeError(
            f"Tools {sorted(ungoverned)} were registered outside harness governance; a harness "
            "Strands agent takes only harness tools, TemporalMCPClient tools and a "
            "TemporalSandbox's tools."
        )
    return agent


def _tool_label(tool: Any) -> str:
    return str(getattr(tool, "tool_name", None) or getattr(tool, "__name__", None) or repr(tool))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_result(tool_id: str, status: ToolResultStatus, text: str) -> ToolResult:
    return ToolResult(toolUseId=tool_id, status=status, content=[{"text": text}])


def _result_text(result: ToolResult) -> str:
    return "\n".join(
        block["text"] for block in result.get("content", []) if "text" in block
    ) or json.dumps(result.get("content", []), default=str)


def _with_steering(runner: AgentWorkflowRunner, tool_id: str, output: str) -> str:
    """``output`` with any queued steering appended, claimed for this tool call."""
    steering = runner.claim_steering(tool_id=tool_id)
    return steering.append_to(output) if steering is not None else output


def _stringify(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, BaseModel):
        return result.model_dump_json()
    if isinstance(result, (dict, list, tuple, int, float, bool)) or result is None:
        return json.dumps(result, default=str)
    return str(result)
