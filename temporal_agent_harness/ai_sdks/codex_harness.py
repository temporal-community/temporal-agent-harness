# ABOUTME: Harness glue for the standalone OpenAI Codex plugin (`temporalio.openai_codex`, from
# temporalio/ai-integrations). The plugin makes the `codex app-server` process durable — segment
# Activities, the conversation held in the workflow, host-run tools — and knows nothing about the
# harness. This module is only the harness half: it routes every tool the model calls through
# `runner.run_tool(...)` (so the harness keeps approvals, `tool_requested` / `tool_start` /
# `tool_end` events, activity-backed tools and `agent.subagent_toolset`), and publishes the turn's
# live events (`reply_delta`, model-interaction spans with token usage) to the harness turn stream.
# Mirrors the structure of `openai_agents_harness.py`: the integration is the plugin, this is glue.

"""Harness integration for the OpenAI Codex agent loop.

The durable work is done by the standalone plugin (``temporalio.openai_codex``); this module adapts it
to the harness. Worker side — harness plugin last::

    client = await Client.connect(
        **connect_config,
        plugins=[CodexHarnessPlugin(), AgentHarnessPlugin(tools=MY_TOOLS)],
    )

Workflow side — a :class:`CodexSession` per agent drives each turn::

    @agent.defn
    class MyCodexAgent:
        @agent.init
        def __init__(self, config: AgentConfig) -> None:
            self._runner = AgentWorkflowRunner(config, stream=WorkflowStream(), ...)
            self._codex = CodexSession(tools=[get_weather], instructions="Be brief.")

        @agent.accepts
        async def ask(self, message: TextMessage) -> TextReply:
            result = await self._codex.run(self._runner, message.text)
            return TextReply(text=result.text)

Codex's own built-in tools are turned off, so every tool the model calls is a harness tool.

.. warning::
    Experimental. The plugin relies on experimental Codex app-server APIs and the rollout file format;
    see its README.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any

_INSTALL_MESSAGE = (
    "Codex support requires the optional `codex` extra. "
    "Install it with `uv sync --extra codex` or "
    "`pip install 'temporal-agent-harness[codex]'`."
)

try:
    from temporalio.common import RetryPolicy
    from temporalio.openai_codex import (
        CodexObserver,
        CodexPlugin,
        CodexTokenUsage,
        CodexTurnResult,
    )
    from temporalio.openai_codex.workflow import (
        CodexPendingCall,
        CodexTool,
        function_schema,
    )
    from temporalio.openai_codex.workflow import CodexSession as _PluginSession
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise RuntimeError(_INSTALL_MESSAGE) from exc

from pydantic_core import to_jsonable_python

from temporal_agent_harness.harness.agent_protocol import (
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    TokenUsage,
    ToolRequested,
)
from temporal_agent_harness.harness.agent_workflow import (
    AgentWorkflowRunner,
    TurnEventPublisher,
)
from temporal_agent_harness.harness.stream_context import TurnStreamContext

__all__ = [
    "CodexHarnessPlugin",
    "CodexSession",
    "CodexTurnResult",
    "as_codex_tool",
    "as_codex_tools",
    "harness_observer_factory",
]

_HARNESS_TOOL_ATTRS = ("__agent_tool__", "__agent_activity_tool__")


# ---------------------------------------------------------------------------
# Live streaming: plugin observer -> harness turn vocabulary
# ---------------------------------------------------------------------------


def _to_token_usage(usage: CodexTokenUsage | None) -> TokenUsage | None:
    if usage is None:
        return None
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        thought_tokens=usage.reasoning_tokens,
        total_tokens=usage.total_tokens,
    )


class _HarnessObserver:
    """Publishes a segment's live events onto the harness turn stream."""

    def __init__(self, publisher: TurnEventPublisher) -> None:
        self._publisher = publisher

    def model_interaction_started(self, model: str | None) -> None:
        self._publisher.publish(ModelInteractionStarted(model=model))

    def reply_delta(self, text: str) -> None:
        self._publisher.publish(ReplyDelta(text=text))

    def model_interaction_ended(
        self, model: str | None, usage: CodexTokenUsage | None
    ) -> None:
        self._publisher.publish(
            ModelInteractionEnded(model=model, usage=_to_token_usage(usage))
        )


@contextlib.asynccontextmanager
async def harness_observer_factory(context: dict[str, Any]) -> AsyncIterator[CodexObserver]:
    """The plugin's ``observer_factory`` for harness agents.

    ``context`` is the in-flight turn's :class:`TurnStreamContext` (:meth:`CodexSession.run` passes
    it), so each segment publishes to the right agent's stream.
    """
    async with AgentWorkflowRunner.publisher_from_activity(
        TurnStreamContext.model_validate(context)
    ) as publisher:
        yield _HarnessObserver(publisher)


class CodexHarnessPlugin(CodexPlugin):
    """The Codex plugin wired for the harness streaming seam. List it BEFORE ``AgentHarnessPlugin``.

    Takes the same arguments as :class:`temporalio.openai_codex.CodexPlugin`, minus
    ``observer_factory`` (it is :func:`harness_observer_factory`).
    """

    def __init__(
        self,
        *,
        codex_bin: str | None = None,
        config_overrides: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        home_root: str | None = None,
    ) -> None:
        super().__init__(
            codex_bin=codex_bin,
            config_overrides=config_overrides,
            env=env,
            home_root=home_root,
            observer_factory=harness_observer_factory,
        )


# ---------------------------------------------------------------------------
# Workflow side: harness tools -> Codex host tools, and the turn driver
# ---------------------------------------------------------------------------


def _require_harness_tool(tool_callable: Callable[..., Any]) -> None:
    if any(getattr(tool_callable, attr, False) for attr in _HARNESS_TOOL_ATTRS):
        return
    name = getattr(tool_callable, "__name__", repr(tool_callable))
    raise TypeError(
        f"{name} is not a harness tool; decorate it with @agent.tool_defn, "
        "@agent.activity_tool_defn, or use agent.subagent_toolset(...)."
    )


def _stringify(result: Any) -> str:
    import json

    if isinstance(result, str):
        return result
    return json.dumps(to_jsonable_python(result))


def _as_codex_tool(
    get_runner: Callable[[], AgentWorkflowRunner],
    tool_callable: Callable[..., Awaitable[Any]],
) -> CodexTool:
    _require_harness_tool(tool_callable)
    schema = function_schema(tool_callable)

    async def handler(call: CodexPendingCall) -> str:
        runner = get_runner()
        if runner.current_stream_context is not None:
            runner.publish(
                ToolRequested(
                    tool_id=call.call_id, tool_name=call.tool, tool_input=call.arguments
                )
            )
        kwargs = schema.parse(call.arguments)
        return _stringify(await runner.run_tool(call.call_id, tool_callable, **kwargs))

    return CodexTool(spec=schema.spec, handler=handler)


def as_codex_tool(
    runner: AgentWorkflowRunner, tool_callable: Callable[..., Awaitable[Any]]
) -> CodexTool:
    """Adapt a harness tool into a Codex host tool bound to ``runner``.

    ``tool_callable`` must come from :func:`harness.agent.tool_defn`,
    :func:`harness.agent.activity_tool_defn` or :func:`harness.agent.subagent_toolset`. Every call
    runs through ``runner.run_tool(...)``, so the harness owns approval-policy evaluation,
    ``tool_start`` / ``tool_end`` / ``tool_error`` events, ``Injected`` parameters and
    activity-backed execution. Use this to drive the plugin's own ``CodexSession`` directly; most
    agents use :class:`CodexSession` below, which does it for them.
    """
    return _as_codex_tool(lambda: runner, tool_callable)


def as_codex_tools(
    runner: AgentWorkflowRunner,
    tool_callables: Mapping[str, Callable[..., Awaitable[Any]]]
    | Sequence[Callable[..., Awaitable[Any]]],
) -> list[CodexTool]:
    """Adapt several harness tools (for example the result of ``agent.subagent_toolset``)."""
    tools = (
        list(tool_callables.values())
        if isinstance(tool_callables, Mapping)
        else list(tool_callables)
    )
    return [as_codex_tool(runner, t) for t in tools]


class CodexSession:
    """One Codex thread, owned by an agent workflow. Keep it on the workflow instance.

    A harness-flavored wrapper over the plugin's :class:`~temporalio.openai_codex.workflow.CodexSession`:
    it takes harness tools, runs every call through the runner given to :meth:`run`, and routes the
    turn's live events to the harness stream. The conversation state (``thread_id``, ``rollout``,
    ``tool_results``) is the plugin session's.

    Args:
        tools: Harness tools the model may call.
        instructions: Developer instructions for the thread (applied when it starts).
        model: Model id to request for the thread; defaults to Codex's own default.
        cwd: Working directory for the app-server; defaults to a scratch directory.
        sandbox: Codex sandbox preset for the thread (built-in tools are off, so this only matters
            if you re-enable some).
        continue_prompt: The user message sent to resume the turn after each tool call.
        max_segments: Safety bound on tool round trips in one turn.
        segment_timeout: Start-to-close timeout of each segment Activity.
        heartbeat_timeout: Heartbeat timeout of each segment Activity.
        retry_policy: Retry policy of each segment Activity.
    """

    def __init__(
        self,
        *,
        tools: Sequence[Callable[..., Awaitable[Any]]] = (),
        instructions: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
        sandbox: str = "read-only",
        continue_prompt: str = "Continue.",
        max_segments: int = 50,
        segment_timeout: timedelta = timedelta(minutes=5),
        heartbeat_timeout: timedelta = timedelta(seconds=30),
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._runner: AgentWorkflowRunner | None = None
        self._session = _PluginSession(
            tools=[_as_codex_tool(self._current_runner, t) for t in tools],
            instructions=instructions,
            model=model,
            cwd=cwd,
            sandbox=sandbox,
            continue_prompt=continue_prompt,
            max_segments=max_segments,
            segment_timeout=segment_timeout,
            heartbeat_timeout=heartbeat_timeout,
            retry_policy=retry_policy,
        )

    def _current_runner(self) -> AgentWorkflowRunner:
        if self._runner is None:  # pragma: no cover - run() always sets it first
            raise RuntimeError("CodexSession.run(runner, ...) was not called")
        return self._runner

    @property
    def thread_id(self) -> str | None:
        """The Codex thread id, once the first segment has started it."""
        return self._session.thread_id

    @property
    def rollout(self) -> str:
        """The committed rollout (Codex's append-only conversation log)."""
        return self._session.rollout

    @property
    def tool_results(self) -> dict[str, str]:
        """Output of every host tool call so far, by call id."""
        return self._session.tool_results

    async def run(self, runner: AgentWorkflowRunner, prompt: str) -> CodexTurnResult:
        """Run one turn: ``prompt`` in, Codex's final answer out."""
        self._runner = runner
        context = runner.current_stream_context
        return await self._session.run(
            prompt,
            observer_context=context.model_dump(mode="json") if context is not None else None,
        )
