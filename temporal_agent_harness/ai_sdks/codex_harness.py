# ABOUTME: Harness integration for the OpenAI Codex agent loop (`codex app-server`). Unlike the
# OpenAI Agents / Gemini / Pydantic AI integrations, Codex is not a model-calling library: its
# agent loop runs in an external `codex app-server` process (JSON-RPC over stdio). So instead of
# wrapping model calls, this adapter wraps the *process*: one Activity per "segment" of a turn,
# the conversation (Codex's append-only rollout) held in the workflow, and every tool the model
# calls provided by the HOST as a Codex dynamic tool, so it runs through `runner.run_tool(...)` —
# the harness stays responsible for approvals, tool lifecycle events, activity-backed tools and
# subagents (`agent.subagent_toolset`). Codex's own built-in tools (shell, apply_patch, ...) are
# turned off, so nothing a turn does happens outside a harness tool.

"""Harness integration for the OpenAI Codex agent loop.

Worker side — register the segment Activity with the plugin (harness plugin last)::

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

How a turn works. Each segment is one Activity: it rebuilds a fresh ``CODEX_HOME`` from the
rollout the workflow holds, starts ``codex app-server``, resumes the thread, injects the real
output of the previous tool call, and runs the turn. When the model calls a host tool, the
segment ends (the app-server is killed — see ``_run_segment``) and returns the rollout lines it
appended; the workflow runs the tool through the harness and starts the next segment. Because the
conversation lives in the workflow, a retry of a segment — on any Worker — resumes from the last
committed rollout, and a tool that already ran is never run again.

.. warning::
    Experimental. This relies on Codex app-server APIs that are themselves experimental
    (``dynamicTools``, ``thread/inject_items``) and on the rollout file format; pin the Codex
    version (the ``codex`` extra pins ``openai-codex-cli-bin``).
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import inspect
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model
from pydantic_core import to_jsonable_python
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.plugin import SimplePlugin

from temporal_agent_harness.harness.agent_protocol import (
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    TokenUsage,
    ToolRequested,
)
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
from temporal_agent_harness.harness.stream_context import TurnStreamContext

__all__ = [
    "CODEX_RUN_SEGMENT_ACTIVITY",
    "CodexActivities",
    "CodexHarnessPlugin",
    "CodexPendingCall",
    "CodexSegmentInput",
    "CodexSegmentResult",
    "CodexSession",
    "CodexTool",
    "CodexToolSpec",
    "CodexTurnResult",
    "as_codex_tool",
    "as_codex_tools",
]

CODEX_RUN_SEGMENT_ACTIVITY = "temporal_agent_harness.codex.run_segment"

_HARNESS_TOOL_ATTRS = ("__agent_tool__", "__agent_activity_tool__")

# Per-thread Codex config that removes its built-in tools, so every tool call is a host tool.
# (`apply_patch` has no feature flag; removing it needs a model-catalog override, not done yet —
# see the example README's caveats.)
_THREAD_CONFIG: dict[str, Any] = {
    "features": {
        "shell_tool": False,
        "view_image": False,
        "goals": False,
        "multi_agent": False,
        "tool_suggest": False,
    },
    "web_search": "disabled",
}


# ---------------------------------------------------------------------------
# Wire types (cross the Activity boundary; Pydantic, like the rest of the harness)
# ---------------------------------------------------------------------------


class CodexToolSpec(BaseModel):
    """A host tool as Codex sees it (a ``dynamicTools`` entry)."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class CodexPendingCall(BaseModel):
    """A host-tool call the model made, awaiting execution by the workflow."""

    call_id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class CodexSegmentInput(BaseModel):
    prompt: str
    thread_id: str | None = None
    rollout_name: str | None = None
    rollout: str = ""
    committed_lines: int = 0
    # call_id -> real tool output, injected into the resumed thread before the turn starts.
    inject: dict[str, str] = Field(default_factory=dict)
    tools: list[CodexToolSpec] = Field(default_factory=list)
    instructions: str | None = None
    model: str | None = None
    cwd: str | None = None
    sandbox: str = "read-only"
    stream_context: TurnStreamContext | None = None


class CodexSegmentResult(BaseModel):
    thread_id: str
    rollout_name: str
    tail: str = Field(description="Rollout lines this segment appended (the workflow appends them).")
    status: Literal["done", "tool_call"]
    final_response: str = ""
    call: CodexPendingCall | None = None
    usage: TokenUsage | None = None


# ---------------------------------------------------------------------------
# Activity side: a minimal asyncio JSON-RPC client for `codex app-server`
# ---------------------------------------------------------------------------
#
# Deliberately not the `openai-codex` SDK client: that client runs server-request handlers on its
# reader thread, so one pending tool call would stall every notification. Here each server request
# is served in its own task, and a call we never answer (a pending host tool) blocks nothing.


class _AppServerExited(RuntimeError):
    pass


def _default_codex_bin() -> str:
    if os.environ.get("CODEX_BIN"):
        return os.environ["CODEX_BIN"]
    try:
        import codex_cli_bin  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "Codex support requires the optional `codex` extra (it ships the Codex binary). "
            "Install it with `uv sync --extra codex` or "
            "`pip install 'temporal-agent-harness[codex]'`, or pass `codex_bin=` / set CODEX_BIN."
        ) from exc
    return str(codex_cli_bin.bundled_codex_path())


class _AppServer:
    def __init__(
        self,
        *,
        codex_bin: str,
        home: str,
        config_overrides: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        on_request: Callable[[str, Any], Awaitable[Any]],
    ) -> None:
        self._argv = [codex_bin]
        for kv in config_overrides:
            self._argv += ["--config", kv]
        self._argv += ["app-server", "--listen", "stdio://"]
        self._env = {**os.environ, **env, "CODEX_HOME": home}
        self._cwd = cwd
        self._on_request = on_request
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.stderr: list[str] = []

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            cwd=self._cwd,
            limit=1 << 24,
        )
        self._readers = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]
        await self.call(
            "initialize",
            {
                "clientInfo": {"name": "temporal-agent-harness", "title": "Temporal Agent Harness", "version": "0"},
                # dynamicTools and thread/inject_items are experimental protocol surface.
                "capabilities": {"experimentalApi": True},
            },
        )
        self._send({"method": "initialized"})

    async def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        async for line in self.proc.stderr:
            self.stderr.append(line.decode(errors="replace").rstrip())
            del self.stderr[:-50]

    def _send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode())

    async def call(self, method: str, params: Any = None) -> Any:
        request_id = str(uuid.uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        reply = await future
        if "error" in reply:
            raise RuntimeError(f"codex {method}: {reply['error']}")
        return reply["result"]

    async def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        async for line in self.proc.stdout:
            message = json.loads(line)
            if "method" in message and "id" in message:
                task = asyncio.create_task(self._serve(message))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            elif "method" in message:
                self.notifications.put_nowait(message)
            else:
                future = self._pending.pop(message["id"], None)
                if future is not None and not future.done():
                    future.set_result(message)
        self.notifications.put_nowait({"method": "__eof__"})
        for future in self._pending.values():
            if not future.done():
                future.set_exception(_AppServerExited("codex app-server exited"))

    async def _serve(self, message: dict[str, Any]) -> None:
        try:
            result = await self._on_request(message["method"], message.get("params"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to Codex as a JSON-RPC error
            self._send({"id": message["id"], "error": {"code": -32000, "message": str(exc)}})
            return
        self._send({"id": message["id"], "result": result})

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            self.proc.kill()

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                assert self.proc.stdin is not None
                self.proc.stdin.close()
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
        for reader in self._readers:
            reader.cancel()


def _rollout_files(home: str, thread_id: str) -> list[str]:
    return glob.glob(f"{home}/sessions/**/rollout-*-{thread_id}.jsonl", recursive=True)


def _token_usage(params: Mapping[str, Any]) -> TokenUsage | None:
    last = ((params.get("tokenUsage") or {}).get("last")) or None
    if not last:
        return None
    return TokenUsage(
        input_tokens=last.get("inputTokens"),
        output_tokens=last.get("outputTokens"),
        thought_tokens=last.get("reasoningOutputTokens"),
        total_tokens=last.get("totalTokens"),
    )


class CodexActivities:
    """Worker-side configuration + the segment Activity.

    Everything that must not enter workflow history lives here, on the Worker: the Codex
    binary, model-provider config, and environment (API keys).

    Args:
        codex_bin: Path to the ``codex`` binary. Defaults to ``$CODEX_BIN`` or the binary bundled
            with ``openai-codex-cli-bin`` (the ``codex`` extra).
        config_overrides: ``--config key=value`` pairs passed to ``codex`` (for example a custom
            ``model_provider``). Per-process, so they apply to every thread.
        env: Extra environment variables for the app-server process (for example
            ``OPENAI_API_KEY``). The Worker's own environment is inherited.
        home_root: Directory under which each segment gets a fresh ``CODEX_HOME``. Defaults to
            the system temp directory.
    """

    def __init__(
        self,
        *,
        codex_bin: str | None = None,
        config_overrides: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        home_root: str | None = None,
    ) -> None:
        self._codex_bin = codex_bin
        self._config_overrides = tuple(config_overrides)
        self._env = dict(env or {})
        self._home_root = home_root

    @activity.defn(name=CODEX_RUN_SEGMENT_ACTIVITY)
    async def run_segment(self, inp: CodexSegmentInput) -> CodexSegmentResult:
        """Run one segment of a Codex turn. See the module notes for the lifecycle."""
        home = tempfile.mkdtemp(prefix="codex-home-", dir=self._home_root)
        try:
            return await self._run_segment(inp, home)
        except _AppServerExited as exc:
            raise ApplicationError(str(exc), type="CodexAppServerExited") from exc
        finally:
            shutil.rmtree(home, ignore_errors=True)

    async def _run_segment(self, inp: CodexSegmentInput, home: str) -> CodexSegmentResult:
        # 1. Rehydrate the committed rollout into the fresh CODEX_HOME (Codex resumes from just
        #    this file; no other state is needed).
        if inp.thread_id:
            assert inp.rollout_name is not None
            directory = os.path.join(home, "sessions", "2026", "01", "01")
            os.makedirs(directory)
            with open(os.path.join(directory, inp.rollout_name), "w") as f:
                f.write(inp.rollout)

        pending_calls: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def on_request(method: str, params: Any) -> Any:
            if method == "item/tool/call":
                pending_calls.put_nowait(params)
                # Never answered: the workflow runs the tool and the next segment injects the
                # result. Answering here would make Codex record an output we then duplicate.
                await asyncio.Event().wait()
            raise RuntimeError(f"unsupported server request {method!r}")

        cwd = inp.cwd or home
        server = _AppServer(
            codex_bin=self._codex_bin or _default_codex_bin(),
            home=home,
            config_overrides=self._config_overrides,
            env=self._env,
            cwd=cwd,
            on_request=on_request,
        )
        await server.start()
        try:
            # 2. Start or resume the thread.
            if inp.thread_id:
                thread_id = inp.thread_id
                # The thread config does NOT persist across a resume: without it Codex's built-in
                # tools (shell, apply_patch, goals, ...) come back. Pass it every time.
                await server.call(
                    "thread/resume",
                    {
                        "threadId": thread_id,
                        "cwd": cwd,
                        "approvalPolicy": "never",
                        "sandbox": inp.sandbox,
                        "config": _THREAD_CONFIG,
                    },
                )
                # 3. Replace Codex's synthetic "aborted" output for the paused call with the real one.
                for call_id, output in inp.inject.items():
                    await server.call(
                        "thread/inject_items",
                        {
                            "threadId": thread_id,
                            "items": [{"type": "function_call_output", "call_id": call_id, "output": output}],
                        },
                    )
            else:
                params: dict[str, Any] = {
                    "cwd": cwd,
                    "approvalPolicy": "never",
                    "sandbox": inp.sandbox,
                    # Self-contained rollouts: required to rehydrate a thread on another Worker.
                    "historyMode": "legacy",
                    "config": _THREAD_CONFIG,
                    "dynamicTools": [
                        {"type": "function", "name": t.name, "description": t.description, "inputSchema": t.input_schema}
                        for t in inp.tools
                    ],
                }
                if inp.instructions:
                    params["developerInstructions"] = inp.instructions
                if inp.model:
                    params["model"] = inp.model
                started = await server.call("thread/start", params)
                thread_id = started["thread"]["id"]

            # 4. Run the turn, streaming to the harness turn stream when there is one.
            publisher_cm = (
                AgentWorkflowRunner.publisher_from_activity(inp.stream_context)
                if inp.stream_context is not None
                else contextlib.nullcontext(None)
            )
            async with publisher_cm as publisher:
                if publisher is not None:
                    publisher.publish(ModelInteractionStarted(model=inp.model))
                turn = await server.call(
                    "turn/start",
                    {"threadId": thread_id, "input": [{"type": "text", "text": inp.prompt}]},
                )
                final = ""
                usage: TokenUsage | None = None

                async def pump() -> None:
                    nonlocal final, usage
                    while True:
                        note = await server.notifications.get()
                        method = note["method"]
                        activity.heartbeat(method)
                        if method == "__eof__":
                            raise _AppServerExited(
                                "codex app-server exited mid-turn: " + " | ".join(server.stderr[-3:])
                            )
                        params = note.get("params") or {}
                        if method == "item/agentMessage/delta" and publisher is not None:
                            publisher.publish(ReplyDelta(text=params.get("delta", "")))
                        elif method == "thread/tokenUsage/updated":
                            usage = _token_usage(params) or usage
                        elif method == "item/completed":
                            item = params.get("item") or {}
                            if item.get("type") == "agentMessage" and item.get("text"):
                                final = item["text"]
                        elif method == "turn/completed":
                            return

                pump_task = asyncio.create_task(pump())
                call_task = asyncio.create_task(pending_calls.get())
                done, _ = await asyncio.wait({pump_task, call_task}, return_when=asyncio.FIRST_COMPLETED)
                call: dict[str, Any] | None = None
                if call_task in done:
                    call = call_task.result()
                    # Pause by killing the app-server, NOT turn/interrupt: an interrupt makes Codex
                    # record its own "aborted by user" output for the pending call, which would
                    # duplicate the real result injected next. A kill leaves a clean dangling
                    # function_call (no output) in the rollout.
                    pump_task.cancel()
                    server.kill()
                    await server.proc.wait()
                else:
                    call_task.cancel()
                    pump_task.result()  # re-raises if the app-server exited
                if publisher is not None:
                    publisher.publish(ModelInteractionEnded(model=inp.model, usage=usage))
        finally:
            await server.close()

        # 5. The process is gone (rollout flushed): ship only the lines this segment appended.
        files = _rollout_files(home, thread_id)
        if len(files) != 1:
            raise ApplicationError(f"expected one rollout for thread {thread_id}, found {files}")
        with open(files[0]) as f:
            lines = f.read().splitlines()
        return CodexSegmentResult(
            thread_id=thread_id,
            rollout_name=os.path.basename(files[0]),
            tail="".join(line + "\n" for line in lines[inp.committed_lines :]),
            status="tool_call" if call else "done",
            final_response=final,
            call=(
                CodexPendingCall(call_id=call["callId"], tool=call["tool"], arguments=call.get("arguments") or {})
                if call
                else None
            ),
            usage=usage,
        )


class CodexHarnessPlugin(SimplePlugin):
    """Registers the Codex segment Activity on a worker. List it BEFORE ``AgentHarnessPlugin``.

    Takes the same arguments as :class:`CodexActivities`.
    """

    def __init__(
        self,
        *,
        codex_bin: str | None = None,
        config_overrides: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        home_root: str | None = None,
    ) -> None:
        self._impl = CodexActivities(
            codex_bin=codex_bin, config_overrides=config_overrides, env=env, home_root=home_root
        )

        def add_activities(existing: Sequence[Callable[..., Any]] | None) -> Sequence[Callable[..., Any]]:
            return [*(existing or []), self._impl.run_segment]

        super().__init__(name="CodexHarnessPlugin", activities=add_activities)


# ---------------------------------------------------------------------------
# Workflow side: harness tools -> Codex dynamic tools, and the turn driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodexTool:
    """A harness tool adapted for Codex: its spec plus the callable ``run_tool`` executes."""

    spec: CodexToolSpec
    fn: Callable[..., Awaitable[Any]]
    args_model: type[BaseModel]


def _require_harness_tool(tool_callable: Callable[..., Any]) -> None:
    if any(getattr(tool_callable, attr, False) for attr in _HARNESS_TOOL_ATTRS):
        return
    name = getattr(tool_callable, "__name__", repr(tool_callable))
    raise TypeError(
        f"{name} is not a harness tool; decorate it with @agent.tool_defn, "
        "@agent.activity_tool_defn, or use agent.subagent_toolset(...)."
    )


def as_codex_tool(tool_callable: Callable[..., Awaitable[Any]]) -> CodexTool:
    """Adapt a harness tool into a Codex dynamic tool.

    ``tool_callable`` must come from :func:`harness.agent.tool_defn`,
    :func:`harness.agent.activity_tool_defn` or :func:`harness.agent.subagent_toolset`. Calls
    run through ``runner.run_tool(...)`` (see :meth:`CodexSession.run`), so the harness owns
    approval-policy evaluation, ``tool_start`` / ``tool_end`` / ``tool_error`` events, ``Injected``
    parameters and activity-backed execution. The model-facing schema comes from the tool's
    stamped signature (``self`` and ``Injected[...]`` parameters already stripped).
    """
    _require_harness_tool(tool_callable)
    name = getattr(tool_callable, "__name__", None) or "tool"
    try:
        signature = inspect.signature(tool_callable, eval_str=True)
    except Exception:  # noqa: BLE001 - fall back to the unevaluated signature
        signature = inspect.signature(tool_callable)
    fields: dict[str, Any] = {}
    for param in signature.parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        annotation = Any if param.annotation is inspect.Parameter.empty else param.annotation
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[param.name] = (annotation, default)
    args_model = create_model(f"{name}_args", **fields)
    schema = args_model.model_json_schema()
    schema.pop("title", None)
    spec = CodexToolSpec(
        name=name,
        description=(inspect.getdoc(tool_callable) or "").strip(),
        input_schema=schema,
    )
    return CodexTool(spec=spec, fn=tool_callable, args_model=args_model)


def as_codex_tools(
    tool_callables: Mapping[str, Callable[..., Awaitable[Any]]] | Sequence[Callable[..., Awaitable[Any]]],
) -> list[CodexTool]:
    """Adapt several harness tools (for example the result of ``agent.subagent_toolset``)."""
    tools = list(tool_callables.values()) if isinstance(tool_callables, Mapping) else list(tool_callables)
    return [as_codex_tool(t) for t in tools]


class CodexTurnResult(BaseModel):
    text: str
    usage: TokenUsage | None = None
    segments: int = 1


def _stringify(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(to_jsonable_python(result))


class CodexSession:
    """One Codex thread, owned by an agent workflow. Keep it on the workflow instance.

    Holds the conversation (thread id + committed rollout) so it survives Worker loss, and
    drives each turn: segment Activity -> harness tool -> next segment, until Codex answers.

    Args:
        tools: Harness tools (or :class:`CodexTool`s) the model may call.
        instructions: Developer instructions for the thread (applied when it starts).
        model: Model id to request for the thread; defaults to Codex's own default.
        cwd: Working directory for the app-server; defaults to a scratch directory.
        sandbox: Codex sandbox preset for the thread (built-in tools are off, so this only
            matters if you re-enable some).
        continue_prompt: The user message sent to resume the turn after each tool call.
        max_segments: Safety bound on tool round trips in one turn.
        segment_timeout / heartbeat_timeout / retry_policy: Activity options for a segment.
    """

    def __init__(
        self,
        *,
        tools: Sequence[Callable[..., Awaitable[Any]] | CodexTool] = (),
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
        adapted = [t if isinstance(t, CodexTool) else as_codex_tool(t) for t in tools]
        self._tools: dict[str, CodexTool] = {t.spec.name: t for t in adapted}
        self._instructions = instructions
        self._model = model
        self._cwd = cwd
        self._sandbox = sandbox
        self._continue_prompt = continue_prompt
        self._max_segments = max_segments
        self._segment_timeout = segment_timeout
        self._heartbeat_timeout = heartbeat_timeout
        self._retry_policy = retry_policy or RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1))
        # The conversation: everything a new Worker needs to continue the thread.
        self.thread_id: str | None = None
        self.rollout_name: str | None = None
        self.rollout: str = ""
        self.tool_results: dict[str, str] = {}

    async def run(self, runner: AgentWorkflowRunner, prompt: str) -> CodexTurnResult:
        """Run one turn: ``prompt`` in, Codex's final answer out."""
        context = runner.current_stream_context
        text, inject = prompt, {}
        usage: TokenUsage | None = None
        for segments in range(1, self._max_segments + 1):
            seg = await workflow.execute_activity(
                CODEX_RUN_SEGMENT_ACTIVITY,
                CodexSegmentInput(
                    prompt=text,
                    thread_id=self.thread_id,
                    rollout_name=self.rollout_name,
                    rollout=self.rollout,
                    committed_lines=len(self.rollout.splitlines()),
                    inject=inject,
                    tools=[t.spec for t in self._tools.values()],
                    instructions=self._instructions,
                    model=self._model,
                    cwd=self._cwd,
                    sandbox=self._sandbox,
                    stream_context=context,
                ),
                result_type=CodexSegmentResult,
                start_to_close_timeout=self._segment_timeout,
                heartbeat_timeout=self._heartbeat_timeout,
                retry_policy=self._retry_policy,
            )
            self.thread_id, self.rollout_name = seg.thread_id, seg.rollout_name
            self.rollout += seg.tail
            usage = seg.usage or usage
            if seg.status == "done" or seg.call is None:
                return CodexTurnResult(text=seg.final_response, usage=usage, segments=segments)
            output = await self._run_host_tool(runner, seg.call)
            self.tool_results[seg.call.call_id] = output
            text, inject = self._continue_prompt, {seg.call.call_id: output}
        raise ApplicationError(
            f"Codex turn exceeded max_segments={self._max_segments} tool round trips",
            non_retryable=True,
        )

    async def _run_host_tool(self, runner: AgentWorkflowRunner, call: CodexPendingCall) -> str:
        tool = self._tools.get(call.tool)
        if tool is None:
            return f"Tool {call.tool!r} is not available."
        if runner.current_stream_context is not None:
            runner.publish(ToolRequested(tool_id=call.call_id, tool_name=call.tool, tool_input=call.arguments))
        try:
            parsed = tool.args_model.model_validate(call.arguments)
            kwargs = {name: getattr(parsed, name) for name in type(parsed).model_fields}
            result = await runner.run_tool(call.call_id, tool.fn, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a denied/failed tool is a result the model sees
            return f"Tool {call.tool!r} failed: {exc}"
        return _stringify(result)
