# ABOUTME: Runs Claude Code in a harness agent, with the upstream Claude Agent SDK Temporal plugin
# (`temporalio.claude_agent_sdk`) unchanged. `HarnessClaudeSession` runs one turn per message. It
# gives Claude Code the agent's approval policy (a `PreToolUse` hook), turn-stream events, and
# harness tools (as Claude custom tools).

"""Harness integration for the Claude Agent SDK Temporal plugin.

>>> from claude_agent_sdk import ClaudeAgentOptions
>>> from temporal_agent_harness.ai_sdks.claude_agent_sdk_harness import HarnessClaudeSession
>>> # client: Client.connect(..., plugins=[ClaudeAgentSDKPlugin(), AgentHarnessPlugin()])

>>> @agent.init
... def __init__(self, config: AgentConfig) -> None:
...     self._runner = AgentWorkflowRunner(config, stream=WorkflowStream())
...     self._claude = HarnessClaudeSession(self._runner, ClaudeAgentOptions(), [lookup])

>>> @agent.accepts
... async def ask(self, message: TextMessage) -> TextReply:
...     result = await self._claude.run(message.text)
...     return TextReply(text=result.result or "")

.. warning::
    This integration is experimental and may change in future versions.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, cast

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_json
from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowTopicHandle
from temporalio.exceptions import ApplicationError

_INSTALL_MESSAGE = (
    "Claude Agent SDK support requires the optional `claude-agent-sdk` extra. "
    "Install it with `uv sync --extra claude-agent-sdk` or "
    "`pip install 'temporal-agent-harness[claude-agent-sdk]'`."
)

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ClaudeSDKError,
        CLIConnectionError,
        HookContext,
        HookInput,
        HookJSONOutput,
        HookMatcher,
        McpServerConfig,
        Message,
        PreToolUseHookInput,
        ResultMessage,
        SdkMcpTool,
        StreamEvent,
        SystemInitData,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        create_sdk_mcp_server,
    )
    from temporalio.claude_agent_sdk import TemporalTransport, TransportActivityOptions
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise RuntimeError(_INSTALL_MESSAGE) from exc

from temporal_agent_harness.harness.agent_protocol import (
    AgentStreamItem,
    MessageAccepted,
    MessageDisposition,
    MessageHandlerEnd,
    MessageHandlerError,
    MessageHandlerStart,
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    SubagentMessageSent,
    SubagentReplyReceived,
    SubagentStarted,
    SubagentStopped,
    ThoughtSummaryDelta,
    TokenUsage,
    ToolEndEvent,
    ToolErrorEvent,
    ToolRequested,
    ToolStartEvent,
    TurnEnded,
    TurnStarted,
)
from temporal_agent_harness.harness.agent_workflow import (
    AgentWorkflowRunner,
    ToolApprovalDenied,
    _apply_approval_policy,
    _ApprovalStatus,
    _CallbackStatus,
)
from temporal_agent_harness.harness.stream_context import TurnStreamContext

__all__ = ["HarnessClaudeSession"]

_HARNESS_TOOL_ATTRS = ("__agent_tool__", "__agent_activity_tool__")

# The MCP server the harness tools live under, and Claude Code's prefix for them.
_HARNESS_MCP_SERVER = "harness"
_HARNESS_TOOL_PREFIX = f"mcp__{_HARNESS_MCP_SERVER}__"


# How long Claude Code waits for the hook, which waits for a person's approval. It is long so the
# hook never times out first. It stays under 2**31 ms, past which a JavaScript timer fires at once.
_APPROVAL_TIMEOUT_SECONDS = 20 * 24 * 60 * 60


# The text that continues a turn from its saved prompt, for a session whose last reply is unknown.
_CONTINUE = "Continue from where you left off."

# The inputs of Claude Code's tools that the model writes in its own words: a label for people, and
# a subagent's task. Claude often rewords them when it repeats a call, so calls match without them.
_WORDED_INPUTS = {
    "Bash": {"description"},
    "Agent": {"description", "prompt"},
    "Task": {"description", "prompt"},
}


def _key(name: str, input: dict[str, Any]) -> str:
    """What identifies a call when Claude repeats it after a lost connection."""
    worded = _WORDED_INPUTS.get(name, set())
    return json.dumps(
        [name, {k: v for k, v in input.items() if k not in worded}], sort_keys=True
    )


# The longest wait between attempts to restart a Claude Code that keeps failing.
_MAX_BACKOFF_SECONDS = 5 * 60


@dataclasses.dataclass
class _CutOff:
    """A call a lost connection cut off before its result."""

    tool_id: str  # The id it shows under
    agent_id: str | None  # The agent that made it
    name: str  # Its tool's name, as Claude calls it
    key: str  # Its ``_key``
    child: str | None  # The subagent it started, which a repeat carries on
    end: Callable[[], None]  # Publishes its end, if no repeat takes it over


class _TurnCalls:
    """A turn's gated calls, kept across its connections, so Claude's repeat of a call after a
    lost connection gets the first call's decision, or harness tool result, instead of a new one.

    Each call runs as its own task, so it outlives the connection that started it and the repeat
    can await it. The turn's end cancels any still running, which settles their approvals and
    callbacks.
    """

    def __init__(self) -> None:
        # Each call's task by key, in order: an approval gate's denial, or a harness tool's result.
        self.tasks: dict[str, list[asyncio.Task[Any]]] = {}
        # Each call a lost connection cut off before its result, by its gate.
        self.cut_off: dict[asyncio.Task[Any], _CutOff] = {}
        # The calls whose start was published, so a repeat doesn't publish it again.
        self.started: set[str] = set()
        # Each harness tool call's task: its tool id, name, the subagent that made it, and the
        # ``_pub`` keywords of its events.
        self.harness: dict[
            asyncio.Task[Any], tuple[str, str, str | None, dict[str, str]]
        ] = {}
        # The cut-off calls ended as their subagent ended, which no later call takes over.
        self.ended: set[asyncio.Task[Any]] = set()
        # Each call's full input, and the agent that made it, by its task.
        self.inputs: dict[asyncio.Task[Any], dict[str, Any]] = {}
        self.agents: dict[asyncio.Task[Any], str | None] = {}

    def add(
        self,
        name: str,
        input: dict[str, Any],
        agent_id: str | None,
        task: asyncio.Task[Any],
    ) -> asyncio.Task[Any]:
        self.tasks.setdefault(_key(name, input), []).append(task)
        self.inputs[task] = input
        self.agents[task] = agent_id
        return task

    def claimable(self, task: asyncio.Task[Any], agent_id: str | None) -> bool:
        """Whether ``agent_id``'s repeat of a call can take ``task``: only a call the same agent
        made, and not one its subagent's end already ended."""
        return task not in self.ended and self.agents[task] == agent_id

    async def settle(self) -> None:
        """Cancel the calls no repeat took over, and end the cut-off ones in an error."""
        running = [t for ts in self.tasks.values() for t in ts if not t.done()]
        for task in running:
            task.cancel()
        if running:
            await asyncio.wait(running)
        while self.cut_off:
            self.cut_off.pop(next(iter(self.cut_off))).end()

    def end_cut_off(
        self, runner: AgentWorkflowRunner, which: Callable[[_CutOff], bool]
    ) -> None:
        """End the cut-off calls ``which`` picks, which no repeat will take over. One still
        waiting for approval is denied, as a cancelled call is in the other integrations."""
        for gate, cut in list(self.cut_off.items()):
            if which(cut) and self.cut_off.pop(gate, None):
                self.ended.add(gate)
                status = runner._status
                entry = status.approval_entry(cut.tool_id)
                if entry is not None and entry.status is _ApprovalStatus.PENDING:
                    status.finalize_approval(cut.tool_id, closed=runner._closed)
                    runner._publish_approval_resolved(cut.tool_id)
                cut.end()


class HarnessClaudeSession:
    """A Claude Code session that a harness agent runs one turn per message.

    Every tool call goes through the runner's approval policy. ``tool_callables`` are harness
    tools, which Claude calls as custom tools. ``activity_options`` and ``stream`` go to each
    turn's ``TemporalTransport``.

    ``options.mcp_servers`` must be a dict. The session sets ``permission_mode``,
    ``include_partial_messages``, and ``CLAUDE_CODE_DISABLE_BACKGROUND_TASKS``, which it relies on.
    ``options.thinking`` defaults to adaptive thinking with summaries.
    """

    def __init__(
        self,
        runner: AgentWorkflowRunner,
        options: ClaudeAgentOptions,
        tool_callables: (
            Mapping[str, Callable[..., Awaitable[Any]]]
            | Sequence[Callable[..., Awaitable[Any]]]
        ) = (),
        *,
        injections: Mapping[str, Any] | None = None,
        activity_options: TransportActivityOptions | None = None,
        stream: WorkflowTopicHandle[Any] | None = None,
    ) -> None:
        self._runner = runner
        self._options = dataclasses.replace(
            options,
            # The approval policy decides each call, so Claude Code needs no classifier mode.
            permission_mode="default",
            include_partial_messages=True,
            # Subagents and commands finish within the turn.
            env={**options.env, "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},
            # Newer models omit thinking text unless asked for a summary.
            thinking=options.thinking or {"type": "adaptive", "display": "summarized"},
        )
        if isinstance(tool_callables, Mapping):
            tool_callables = list(tool_callables.values())
        self._claude_tools = [
            _as_claude_tool(runner, tool, injections, lambda: self._observer)
            for tool in tool_callables
        ]
        # The current connection's observer, set before Claude Code can call a harness tool.
        self._observer: _ClaudeStreamObserver
        self._activity_options = activity_options
        self._stream = stream
        # The session, resumed each turn (``options.resume`` to start), and its last reply.
        self._session_id: str | None = options.resume
        self._reply_id: str | None = None

    async def run(self, prompt: str) -> ResultMessage:
        """Run one turn of the session for ``prompt``, and return Claude Code's result.

        Raises ``ApplicationError`` with Claude Code's error text if the turn fails, so the
        handler fails as it does when a model call fails in the other integrations.

        After a lost connection, or Claude Code failing, the turn starts again from its prompt, so
        Claude Code's tools may run again; a repeated call reuses its approval, and a harness tool
        its result.
        """
        session_id, reply_id = self._session_id, self._reply_id  # Where the turn starts
        prompt_id = None  # The prompt's uuid, once Claude Code saved it
        text, resume_at = prompt, None  # What to send, and where to resume
        calls = _TurnCalls()
        failures = 0
        try:
            while True:
                observer, options = self._connection(resume_at, calls)
                try:
                    # A new client each turn, so Claude Code's callbacks publish to this turn.
                    async with ClaudeSDKClient(
                        options,
                        transport=TemporalTransport(
                            options,
                            activity_options=self._activity_options,
                            stream=self._stream,
                        ),
                    ) as client:
                        await client.query(text)
                        async for message in client.receive_response():
                            observer.observe(message)
                            if isinstance(message, AssistantMessage):
                                failures = 0  # Claude replied, so restart at once again.
                            if (
                                isinstance(message, SystemMessage)
                                and message.subtype == "init"
                            ):
                                self._session_id = cast(SystemInitData, message.data)[
                                    "session_id"
                                ]
                            elif (
                                isinstance(message, UserMessage)
                                and message.uuid
                                and not prompt_id
                            ):
                                prompt_id = message.uuid
                            elif (
                                isinstance(message, AssistantMessage)
                                and message.parent_tool_use_id is None
                            ):
                                self._reply_id = message.uuid
                            elif isinstance(message, ResultMessage):
                                if message.is_error or message.subtype.startswith("error"):
                                    # Fails the handler, as a failed model call does; the
                                    # handler below ends what the turn left open.
                                    raise ApplicationError(
                                        message.result
                                        or "\n".join(message.errors or [])
                                        or message.subtype
                                    )
                                return message
                    raise CLIConnectionError("Claude Code exited before the turn's result")
                except ClaudeSDKError as error:
                    # The Worker or Claude Code went away, or Claude Code failed.
                    observer.close(str(error))
                    # Resume at the reply before the turn, or start a new session, and send the
                    # prompt again. That drops any copy of the prompt Claude Code saved, and the
                    # model sees the prompt as the person sent it. A session from options.resume
                    # has no known reply, so it continues from the prompt if it was saved.
                    self._session_id, self._reply_id = session_id, reply_id
                    if not reply_id and session_id and prompt_id:
                        text, resume_at = _CONTINUE, prompt_id
                    else:
                        resume_at = reply_id
                    # Retry at once, then back off, so a Claude Code that can't start waits for
                    # an operator instead of spinning.
                    if failures:
                        await workflow.sleep(
                            min(2**failures, _MAX_BACKOFF_SECONDS),
                            summary="Wait to restart Claude Code",
                        )
                    failures += 1
                except (Exception, asyncio.CancelledError) as error:
                    observer.close(str(error) or type(error).__name__)
                    raise
        finally:
            await calls.settle()

    def _connection(
        self, resume_at: str | None, calls: _TurnCalls
    ) -> tuple[_ClaudeStreamObserver, ClaudeAgentOptions]:
        """A connection's observer, and the options with the harness hooks and tools added."""
        observer = self._observer = _ClaudeStreamObserver(self._runner, calls)
        hooks = self._options.hooks or {}
        return observer, dataclasses.replace(
            self._options,
            hooks={
                **hooks,
                "PreToolUse": [
                    *hooks.get("PreToolUse", []),
                    HookMatcher(
                        hooks=[observer.pre_tool_use],
                        timeout=_APPROVAL_TIMEOUT_SECONDS,
                    ),
                ],
            },
            resume=self._session_id,
            resume_session_at=resume_at,
            # Claude Code sends the prompt back with a uuid, to resume from after a lost connection.
            extra_args={**self._options.extra_args, "replay-user-messages": None},
            mcp_servers={
                **cast(dict[str, McpServerConfig], self._options.mcp_servers),
                _HARNESS_MCP_SERVER: create_sdk_mcp_server(
                    _HARNESS_MCP_SERVER, tools=self._claude_tools
                ),
            },
        )


class _ClaudeStreamObserver:
    """Publishes harness turn-stream events for the messages Claude Code sends in one turn.

    Claude Code reports output tokens only for the whole turn, so the turn's last model call
    carries them. Each Claude Code subagent publishes as a subagent with its own ``agent_id`` and
    turn, inline on this agent's stream.
    """

    def __init__(self, runner: AgentWorkflowRunner, calls: _TurnCalls) -> None:
        self._runner = runner
        self._turn_calls = calls
        # The earlier connections' calls, each taken once, by a repeat of the same call.
        self._earlier = {key: list(tasks) for key, tasks in calls.tasks.items()}
        # Each of Claude Code's calls' task (None until it has one), by tool_use_id.
        self._gates: dict[str, asyncio.Task[Any] | None] = {}
        # The cut-off call each repeat took over, whose events it publishes under that call's id.
        self._took_over: dict[str, str] = {}
        # Each requested call, and the subagent that made it, by tool_use_id.
        self._tools: dict[str, tuple[ToolUseBlock, str | None]] = {}
        # Each requested harness tool call its handler hasn't claimed, and the subagent that made it.
        self._harness_calls: list[tuple[ToolUseBlock, str | None]] = []
        # Each requested harness tool call without its result yet, and the subagent that made it.
        self._harness_open: dict[str, str | None] = {}
        # The tasks of the harness tool calls this connection ran or took over.
        self._harness_tasks: list[asyncio.Task[Any]] = []
        # Each subagent's agent_id, by the tool_use_id of the call that started it, and back.
        self._subagents: dict[str, str] = {}
        self._calls: dict[str, str] = {}
        # Each agent (None for this one) with a model call open, in open order so replay publishes
        # the same events. The value is the end to publish: None until the reply arrives.
        self._open: dict[str | None, ModelInteractionEnded | None] = {}
        # Each agent's model, from its latest reply, for the calls that start or end without one.
        self._models: dict[str | None, str] = {}
        # Claude's replies seen so far. Claude Code splits one reply into an AssistantMessage per
        # content block, each carrying the reply's id and usage.
        self._replies: set[str] = set()
        # The replies whose text and thinking were streamed, so their whole blocks aren't again.
        self._streamed: set[str] = set()
        # The keys of each agent's calls in its latest reply.
        self._reply_keys: dict[str | None, set[str]] = {}

    def observe(self, message: Message) -> None:
        """Publish ``message``'s events."""
        if isinstance(message, SystemMessage) and message.subtype == "init":
            self._start_model_call(None)
        elif isinstance(message, AssistantMessage):
            agent_id = self._agent_id(message.parent_tool_use_id)
            if message.message_id is None or message.message_id not in self._replies:
                if message.message_id is not None:
                    self._replies.add(message.message_id)
                self._end_model_call(agent_id)
                self._reply_keys[agent_id] = set()
                self._models[agent_id] = message.model
                self._start_model_call(agent_id)
                self._open[agent_id] = ModelInteractionEnded(
                    model=message.model, usage=_usage(message.usage)
                )
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    # A tool request is the end of Claude's reply.
                    self._end_model_call(agent_id)
                    keys = self._reply_keys.setdefault(agent_id, set())
                    keys.add(_key(block.name, block.input))
                    # A cut-off call its agent now asks for with another input was replanned, so
                    # no repeat will take it over.
                    self._turn_calls.end_cut_off(
                        self._runner,
                        lambda cut: cut.agent_id == agent_id
                        and cut.name == block.name
                        and cut.key not in keys,
                    )
                    if block.name.startswith(_HARNESS_TOOL_PREFIX):
                        self._harness_calls.append((block, agent_id))
                        self._harness_open[block.id] = agent_id
                    elif (gate := self._claim(block, agent_id)) in self._turn_calls.cut_off:
                        # A repeat of a call a lost connection cut off: it carries on as that
                        # call, and its subagent as that subagent, so the person sees one call,
                        # one approval, and one subagent.
                        cut = self._turn_calls.cut_off.pop(gate)
                        self._tools[block.id] = (block, agent_id)
                        self._took_over[block.id] = cut.tool_id
                        if cut.child is not None:
                            self._subagents[block.id] = cut.child
                            self._calls[cut.child] = cut.tool_id
                    else:
                        self._tools[block.id] = (block, agent_id)
                        # A repeat of a denied call that already ended gets no card of its
                        # own: the person saw that call's, which its denial ended.
                        if not _denial(gate):
                            self._publish(
                                ToolRequested(
                                    tool_id=block.id,
                                    tool_name=block.name,
                                    tool_input=block.input,
                                ),
                                agent_id,
                            )
                elif message.message_id in self._streamed:
                    continue
                elif isinstance(block, TextBlock):
                    self._publish(ReplyDelta(text=block.text), agent_id)
                elif isinstance(block, ThinkingBlock):
                    self._publish(
                        ThoughtSummaryDelta(delta={"content": block.thinking}),
                        agent_id,
                    )
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            # A subagent's first message is its task, which opens its bracket.
            agent_id = self._agent_id(message.parent_tool_use_id)
            for block in message.content:
                if (
                    isinstance(block, ToolResultBlock)
                    and block.tool_use_id in self._tools
                ):
                    self._end_tool(
                        block.tool_use_id,
                        _text(block.content) if block.is_error else None,
                        cast(JsonValue, block.content),
                        message.tool_use_result,
                    )
                elif (
                    isinstance(block, ToolResultBlock)
                    and block.tool_use_id in self._harness_open
                ):
                    del self._harness_open[block.tool_use_id]
                    # A harness call Claude Code answered without its handler, if it did.
                    self._harness_calls = [
                        c for c in self._harness_calls if c[0].id != block.tool_use_id
                    ]
            # Claude answers a subagent's task and its tool results, its harness tools' included,
            # with its next model call, once all its calls have ended.
            if (
                agent_id is not None
                or any(isinstance(block, ToolResultBlock) for block in message.content)
            ) and agent_id not in [
                *(a for _, a in self._tools.values()),
                *self._harness_open.values(),
            ]:
                self._start_model_call(agent_id)
        elif isinstance(message, StreamEvent):
            event = message.event
            agent_id = self._agent_id(message.parent_tool_use_id)
            if event["type"] == "message_start":
                self._streamed.add(event["message"]["id"])
            elif event["type"] == "content_block_delta":
                delta = event["delta"]
                if delta["type"] == "text_delta":
                    self._publish(ReplyDelta(text=delta["text"]), agent_id)
                elif delta["type"] == "thinking_delta":
                    self._publish(
                        ThoughtSummaryDelta(delta={"content": delta["thinking"]}),
                        agent_id,
                    )
        elif isinstance(message, ResultMessage):
            self._end_model_call(None, (message.usage or {}).get("output_tokens"))
            for agent_id in list(self._open):
                self._close_model_call(agent_id)

    def close(self, reason: str) -> None:
        """End the calls, subagents, and model calls a lost connection or failed turn left open.

        A gated call, and the subagent it started, is left open instead, for a repeat on the next
        connection to take over; its agent's end ends it if none does.
        """
        # Harness tool calls first, so a subagent that ends below ends its own.
        for task in self._harness_tasks:
            if not task.done():
                tool_id, name, agent_id, _ = self._turn_calls.harness[task]
                claude_name = _HARNESS_TOOL_PREFIX + name
                self._turn_calls.cut_off[task] = _CutOff(
                    tool_id,
                    agent_id,
                    claude_name,
                    _key(claude_name, self._turn_calls.inputs[task]),
                    None,
                    functools.partial(self._end_harness, task, reason),
                )
        # The latest first, so a subagent's calls end before it does.
        for tool_id in reversed(list(self._tools)):
            call, agent_id = self._tools[tool_id]
            gate = self._gates.get(tool_id)
            if gate is None:
                self._end_tool(tool_id, reason)
                continue
            self._turn_calls.cut_off[gate] = _CutOff(
                self._took_over.get(tool_id, tool_id),
                agent_id,
                call.name,
                _key(call.name, call.input),
                self._subagents.get(tool_id),
                functools.partial(self._end_tool, tool_id, reason),
            )
        for agent_id in list(self._open):
            self._close_model_call(agent_id)

    def _end_tool(
        self,
        tool_id: str,
        error: str | None,
        output: JsonValue = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Publish the end of a call, and of the subagent it started; ``error`` if it failed."""
        call, agent_id = self._tools.pop(tool_id)
        child = self._subagents.get(tool_id)
        shown = self._took_over.get(tool_id, tool_id)
        if child is not None:
            self._turn_calls.end_cut_off(self._runner, lambda cut: cut.agent_id == child)
            self._close_model_call(child)
            # Claude Code's structured result holds the subagent's own report; the block's text
            # wraps it in a preamble and the subagent's id and usage.
            self._publish(
                MessageHandlerError(message=error)
                if error is not None
                else MessageHandlerEnd(
                    output={"text": _text((result or {}).get("content") or output)}
                ),
                child,
            )
            self._publish(TurnEnded(), child, message_id=None)
            self._publish(
                SubagentReplyReceived(
                    **self._bracket(call),
                    outcome="error" if error is not None else "ok",
                    inline=True,
                ),
                agent_id,
            )
            self._publish(SubagentStopped(**self._ids(call), inline=True), agent_id)
        gate = self._gates.get(tool_id)
        if (
            gate is not None
            and shown not in self._turn_calls.started
            and (self._denied(shown) or _denial(gate))
        ):
            # Denied, or its approval abandoned: as in the other integrations, the call never
            # started, so it gets no tool_start or tool_error. An approved one ends in an error.
            return
        if error is not None:
            self._publish(
                ToolErrorEvent(tool_id=shown, tool_name=call.name, message=error),
                agent_id,
            )
        else:
            self._publish(
                ToolEndEvent(tool_id=shown, tool_name=call.name, tool_output=output),
                agent_id,
            )

    async def pre_tool_use(
        self, input: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        """Apply the approval policy to Claude Code's own tool calls; harness tools pass through."""
        call = cast(PreToolUseHookInput, input)
        tool_id = call["tool_use_id"]
        if not call["tool_name"].startswith(_HARNESS_TOOL_PREFIX):
            # For a subagent's call, Claude Code sends the tool request after the hook.
            await workflow.wait_condition(lambda: tool_id in self._tools)
            block, agent_id = self._tools[tool_id]
            gate = self._gates[tool_id] = self._claim(block, agent_id) or self._turn_calls.add(
                block.name,
                block.input,
                agent_id,
                asyncio.create_task(
                    self._decide(tool_id, call["tool_name"], call["tool_input"], agent_id)
                ),
            )
            # The gate is a task of its own, so a lost connection, which cancels the hook,
            # leaves its approval waiting for Claude Code's repeat of the call.
            denial = await asyncio.shield(gate)
            if denial is not None:
                # Claude Code reports the denial as the call's error result.
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": denial,
                    }
                }
            shown = self._took_over.get(tool_id, tool_id)
            if shown not in self._turn_calls.started:
                self._turn_calls.started.add(shown)
                self._publish(
                    ToolStartEvent(
                        tool_id=shown, tool_name=block.name, tool_input=block.input
                    ),
                    agent_id,
                )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }

    def _claim(self, block: ToolUseBlock, agent_id: str | None) -> asyncio.Task[Any] | None:
        """The earlier connections' task for the call ``block`` repeats, if any, taken once."""
        if block.id not in self._gates:
            key = _key(block.name, block.input)
            earlier = [
                t
                for t in self._earlier.get(key, [])
                if self._turn_calls.claimable(t, agent_id)
            ]
            # An exact repeat first, so parallel subagents of one type don't swap.
            task = next(
                (t for t in earlier if self._turn_calls.inputs[t] == block.input),
                earlier[0] if earlier else None,
            )
            if task is not None:
                self._earlier[key].remove(task)
            self._gates[block.id] = task
        return self._gates[block.id]

    async def _decide(
        self,
        tool_id: str,
        name: str,
        input: dict[str, Any],
        agent_id: str | None,
    ) -> str | None:
        """Apply the approval policy to a call, and return its denial, or None if allowed."""
        try:
            await self._runner.run_tool(
                tool_id,
                _apply_approval_policy,
                name,
                input,
                inherently_safe=False,
                **self._scope(agent_id),
            )
        except ToolApprovalDenied as denied:
            # The gate already published tool_approval_resolved(approved=False).
            return str(denied)
        return None

    def _start_model_call(self, agent_id: str | None) -> None:
        if agent_id not in self._open:
            self._open[agent_id] = None
            self._publish(
                ModelInteractionStarted(model=self._models.get(agent_id)), agent_id
            )

    def _end_model_call(
        self, agent_id: str | None, output_tokens: int | None = None
    ) -> None:
        """Publish the end of ``agent_id``'s replied model call, if it has one open."""
        ended = self._open.get(agent_id)
        if ended is None:
            return
        del self._open[agent_id]
        if ended.usage is not None:
            ended.usage.output_tokens = output_tokens
        self._publish(ended, agent_id)

    def _close_model_call(self, agent_id: str | None) -> None:
        """End ``agent_id``'s open model call, without usage if no reply was seen."""
        self._end_model_call(agent_id)
        if agent_id in self._open:
            del self._open[agent_id]
            self._publish(
                ModelInteractionEnded(model=self._models.get(agent_id)), agent_id
            )

    def _agent_id(self, parent_tool_use_id: str | None) -> str | None:
        """The agent_id of the subagent that ``parent_tool_use_id`` started, or ``None`` for this
        agent. Starts the subagent and its turn the first time, as a child workflow's would be."""
        if parent_tool_use_id is None:
            return None
        if parent_tool_use_id not in self._subagents:
            call, parent = self._tools[parent_tool_use_id]
            child = self._runner._fresh_subagent_handle()
            self._subagents[parent_tool_use_id] = child
            self._calls[child] = self._took_over.get(parent_tool_use_id, parent_tool_use_id)
            # Its events go against its own first turn.
            self._runner._inline_turns[child] = (f"{child}-turn-1", 1)
            self._publish(SubagentStarted(**self._ids(call), inline=True), parent)
            self._publish(
                SubagentMessageSent(**self._bracket(call), inline=True), parent
            )
            self._publish(
                MessageAccepted(
                    handler=call.name,
                    payload={"prompt": call.input.get("prompt", "")},
                    disposition=MessageDisposition.OPENED,
                ),
                child,
            )
            self._publish(TurnStarted(), child, message_id=None)
            self._publish(MessageHandlerStart(), child)
        return self._subagents[parent_tool_use_id]

    def _ids(self, call: ToolUseBlock) -> dict[str, Any]:
        """The fields every marker of the subagent ``call`` started shares."""
        return {
            "subagent_id": self._subagents[call.id],
            "agent_key": call.input.get("subagent_type") or "general-purpose",
            "workflow_id": self._took_over.get(call.id, call.id),
        }

    def _bracket(self, call: ToolUseBlock) -> dict[str, Any]:
        """The fields a subagent's two bracket markers share. It runs one turn."""
        return {**self._ids(call), "handler": call.name, "subagent_turn": 1}

    async def harness_call(
        self,
        name: str,
        args: dict[str, Any],
        run: Callable[[str, dict[str, str]], Awaitable[Any]],
    ) -> Any:
        """Run a harness tool call as the agent that made it, matched by name and input, and
        return its result. ``run`` takes the call's tool id and ``run_tool`` keywords.

        The call runs as a task of its own, so a lost connection, which cancels the handler,
        leaves it running for Claude Code's repeat of the call, which gets its result.
        """
        claude_name = _HARNESS_TOOL_PREFIX + name

        def pending() -> int | None:
            return next(
                (
                    index
                    for index, (block, _) in enumerate(self._harness_calls)
                    if block.name == claude_name and block.input == args
                ),
                None,
            )

        # The handler can run before the observer sees Claude's request.
        await workflow.wait_condition(lambda: pending() is not None)
        block, agent_id = self._harness_calls.pop(cast(int, pending()))
        calls = self._turn_calls
        task = self._claim(block, agent_id)
        if task is not None:
            calls.cut_off.pop(task, None)
        else:
            # claude_agent_sdk passes a custom tool only its arguments, not Claude's tool_use id.
            tool_id = f"claude_agent_sdk:{name}:{workflow.uuid4()}"
            scope = self._scope(agent_id)
            self._publish(
                ToolRequested(tool_id=tool_id, tool_name=name, tool_input=args),
                agent_id,
            )
            task = calls.add(
                claude_name,
                args,
                agent_id,
                asyncio.create_task(self._run_harness(tool_id, name, scope, run)),
            )
            calls.harness[task] = (tool_id, name, agent_id, scope)
        self._harness_tasks.append(task)
        return await asyncio.shield(task)

    async def _run_harness(
        self,
        tool_id: str,
        name: str,
        scope: dict[str, str],
        run: Callable[[str, dict[str, str]], Awaitable[Any]],
    ) -> Any:
        try:
            return await run(tool_id, scope)
        except asyncio.CancelledError:
            # A turn that ends first cancels the call, which the tool doesn't end itself. One
            # whose approval was abandoned never started, as in the other integrations.
            if asyncio.current_task() not in self._turn_calls.ended and not self._denied(
                tool_id
            ):
                self._publish(
                    ToolErrorEvent(tool_id=tool_id, tool_name=name, message="cancelled"),
                    None,
                    **scope,
                )
            raise

    def _end_harness(self, task: asyncio.Task[Any], reason: str) -> None:
        """End a cut-off harness tool call no repeat took over, as its subagent ends: fail its
        callback if it waits for one, publish its error unless it never started, and cancel it."""
        if task.done():
            return  # It ended itself.
        tool_id, name, _, scope = self._turn_calls.harness[task]
        status = self._runner._status
        entry = status.callback_entry(tool_id)
        if entry is not None and entry.status is _CallbackStatus.PENDING:
            status.finalize_callback(tool_id, closed=self._runner._closed)
            self._runner._publish_callback_resolved(tool_id)
        if not self._denied(tool_id):
            self._publish(
                ToolErrorEvent(tool_id=tool_id, tool_name=name, message=reason), None, **scope
            )
        task.cancel()

    def _denied(self, tool_id: str) -> bool:
        """Whether the call's approval was denied, so it never started."""
        entry = self._runner._status.approval_entry(tool_id)
        return entry is not None and entry.status is _ApprovalStatus.DENIED

    def _scope(self, agent_id: str | None) -> dict[str, str]:
        """The ``_pub`` and ``run_tool`` keywords for ``agent_id``'s events; none for this agent."""
        # A subagent's message is the call that started it, so its reply never pairs with this
        # agent's own message.
        if agent_id is None:
            return {}
        return {"agent_id": agent_id, "message_id": self._calls[agent_id]}

    def _publish(
        self, event: AgentStreamItem, agent_id: str | None, /, **scope: str | None
    ) -> None:
        """Publish ``event`` as ``agent_id``'s; ``scope`` overrides its ``_pub`` keywords."""
        # Always set: the observer runs inside the turn's handler.
        ctx = cast(TurnStreamContext, self._runner.current_stream_context)
        self._runner._pub(
            ctx.turn_id, ctx.turn_number, event, **{**self._scope(agent_id), **scope}
        )


def _as_claude_tool(
    runner: AgentWorkflowRunner,
    tool_callable: Callable[..., Awaitable[Any]],
    injections: Mapping[str, Any] | None,
    observer: Callable[[], _ClaudeStreamObserver],
) -> SdkMcpTool[Any]:
    """A Claude custom tool that runs a harness tool through ``runner.run_tool``."""
    if not any(getattr(tool_callable, attr, False) for attr in _HARNESS_TOOL_ATTRS):
        raise TypeError(
            f"{tool_callable!r} is not a harness tool; decorate it with @agent.tool_defn or "
            "@agent.activity_tool_defn."
        )
    name = tool_callable.__name__

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        async def run(tool_id: str, scope: dict[str, str]) -> Any:
            return await runner.run_tool(
                tool_id, tool_callable, injections=injections, **scope, **args
            )

        # A raised failure, a denial included, reaches Claude as the SDK's error result.
        result = await observer().harness_call(name, args, run)
        text = result if isinstance(result, str) else to_json(result).decode()
        return {"content": [{"type": "text", "text": text}]}

    return SdkMcpTool(
        name=name,
        description=(tool_callable.__doc__ or "").strip(),
        # The decorators give the tool a model-facing signature, without self and injections.
        input_schema=TypeAdapter(tool_callable).json_schema(),
        handler=handler,
    )


def _denial(gate: asyncio.Task[Any] | None) -> bool:
    """Whether ``gate`` is an approval gate that denied its call, or abandoned its approval."""
    return (
        gate is not None
        and gate.done()
        and not gate.cancelled()
        and gate.result() is not None
    )


def _text(content: str | list[dict[str, Any]] | None) -> str:
    """A tool result's text: the string, or its text blocks joined."""
    if isinstance(content, str):
        return content
    return "\n".join(
        item["text"] for item in content or [] if item.get("type") == "text"
    )


def _usage(usage: dict[str, Any] | None) -> TokenUsage | None:
    """A Claude reply's input token counts, cached input included. Its ``output_tokens`` is a
    placeholder, so it is left out."""
    if usage is None:
        return None
    cached = usage.get("cache_read_input_tokens") or 0
    return TokenUsage(
        input_tokens=usage.get("input_tokens", 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + cached,
        cached_tokens=cached,
    )
