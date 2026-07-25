# ABOUTME: Proves the SDK-neutral sandbox seam reaches a Pydantic AI agent, which is the whole point
# of it being SDK-neutral. Wires it exactly as an author would — the same shape as
# examples/pydantic_ai_hello — and drives a real TemporalAgent on the time-skipping test server:
#
#   * the tool is a normal harness tool declaring ``sandbox: Injected[SandboxHandle]``;
#   * ``build_harness_toolset(..., injections={"sandbox": SandboxRef("workspace")})`` is built ONCE at
#     module load, so the injection mapping is static — the case that made a per-run sandbox
#     impossible before LazyInjection;
#   * ``attach_sandbox`` supplies the per-run part in the workflow;
#   * the model is a FunctionModel, so this needs no API key and no network.
#
# What it establishes: nothing in the sandbox path is OpenAI-Agents-specific. There is no RunConfig,
# no SandboxRunConfig, and no openai-agents import anywhere in this file — the handle arrives because
# ``run_tool`` resolved it, which every SDK integration goes through.
#
# Streaming is deliberately not wired here (no event_stream_handler): this is about delivery, and the
# streaming seam is covered by tests/ai_sdks/pydantic_ai/test_pydantic_stream_observer.py.
#
# Run with: uv run pytest tests/ai_sdks/pydantic_ai/test_sandbox_injection.py -v

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Mapping

import pytest_asyncio
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin, TemporalAgent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.ai_sdks.pydantic_ai_harness import HarnessDeps, build_harness_toolset
from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent import Injected
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.sandbox import (
    ExecResult,
    FsEntry,
    SandboxHandle,
    SandboxOptions,
    SandboxProvider,
    SandboxRef,
    SandboxState,
    attach_sandbox,
)

PROVIDER = "workspace"
TOOLSET_ID = "sandbox-tools"
AGENT_NAME = "pydantic-sandbox-probe"


# ---------------------------------------------------------------------------
# A minimal backend: one in-memory workspace, hydrated by reference
# ---------------------------------------------------------------------------


class ProbeOptions(SandboxOptions):
    workspace_id: str


class ProbeBackend:
    """Enough of a backend to prove the wiring: an in-memory filesystem and a grep."""

    options_model = ProbeOptions

    def __init__(self, source: Mapping[str, bytes]) -> None:
        self.source = dict(source)
        self.files: dict[str, bytes] = {}
        self.creates = 0
        self.hydrates = 0

    async def create(self, options: ProbeOptions) -> SandboxState:
        self.creates += 1
        return SandboxState(backend_ref=options.workspace_id)

    async def resume(self, state: SandboxState) -> SandboxState:
        return state

    async def delete(self, state: SandboxState) -> None:
        self.files.clear()

    async def exec(
        self, state: SandboxState, command: list[str], timeout: float | None = None
    ) -> ExecResult:
        if command[:2] == ["grep", "-rn"]:
            pattern = command[2]
            hits = [
                f"{path}:{i + 1}:{line}"
                for path, blob in sorted(self.files.items())
                for i, line in enumerate(blob.decode().splitlines())
                if pattern in line
            ]
            return ExecResult(stdout="\n".join(hits), exit_code=0 if hits else 1)
        return ExecResult(stdout=" ".join(command))

    async def run_code(
        self,
        state: SandboxState,
        code: str,
        language: str = "python",
        timeout: float | None = None,
    ) -> ExecResult:
        return ExecResult(stdout=f"{language}:{len(code)}")

    async def read(self, state: SandboxState, path: str) -> bytes:
        return self.files[path]

    async def write(self, state: SandboxState, files: Mapping[str, bytes]) -> int:
        self.files.update(files)
        return sum(len(v) for v in files.values())

    async def ls(self, state: SandboxState, path: str, depth: int = 1) -> list[FsEntry]:
        return [FsEntry(path=p, size=len(b)) for p, b in sorted(self.files.items())]

    async def running(self, state: SandboxState) -> bool:
        return True

    async def hydrate(self, state: SandboxState, locator: str | None = None) -> int:
        """By reference: the backend reads the source itself, so no bytes crossed the boundary."""
        self.hydrates += 1
        self.files.update(self.source)
        return len(self.source)

    async def persist(self, state: SandboxState, locator: str | None = None) -> str:
        self.source.update(self.files)
        return locator or f"probe://{state.backend_ref}"


BACKEND = ProbeBackend(
    {
        "notes.md": b"line one\nthe needle is here\nline three\n",
        "other.md": b"nothing relevant\n",
    }
)


# ---------------------------------------------------------------------------
# The tool + the durable agent, both built once at module load
# ---------------------------------------------------------------------------


@agent.tool_defn(inherently_safe=True)
async def grep_workspace(sandbox: Injected[SandboxHandle], pattern: str) -> str:
    """Search the workspace for `pattern`, returning matching lines."""
    result = await sandbox.exec("grep", "-rn", pattern, ".")
    return result.stdout or f"no matches for {pattern!r}"


# The static mapping is the crux: SandboxRef is a declaration, not a live handle, so one
# module-level toolset serves every concurrent run and each resolves its own sandbox.
_TOOLSET, _TOOL_CONFIG = build_harness_toolset(
    [grep_workspace],
    id=TOOLSET_ID,
    injections={"sandbox": SandboxRef(PROVIDER)},
)


def _script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Call the tool on the first pass, then answer with what it returned.

    Stands in for a model so the test needs no API key. Asserting on the tool's output through the
    reply is what proves the sandbox actually ran: the text could not exist otherwise.
    """
    for message in reversed(messages):
        for part in message.parts:
            if isinstance(part, ToolReturnPart):
                return ModelResponse(parts=[TextPart(content=f"found: {part.content}")])
    return ModelResponse(
        parts=[ToolCallPart(tool_name="grep_workspace", args={"pattern": "needle"})]
    )


_TEMPORAL_AGENT = TemporalAgent(
    Agent(
        FunctionModel(_script),
        deps_type=HarnessDeps,
        toolsets=[_TOOLSET],
    ),
    name=AGENT_NAME,
    # Harness tools must run in-workflow: that is where the approval gate, the tool lifecycle
    # events, and — for a sandbox — execute_activity all live.
    tool_activity_config=_TOOL_CONFIG,
)


@workflow.defn(name="PydanticSandboxProbeAgent")
@agent.defn
class PydanticSandboxProbeAgent:
    """A Pydantic AI agent whose single tool needs a sandbox."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        # The per-run half of the injection: what SandboxRef(PROVIDER) resolves to for THIS run.
        attach_sandbox(self._runner, PROVIDER, ProbeOptions(workspace_id="ws-1"))
        self._last_output = ""

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @workflow.query
    def last_output(self) -> str:
        """The model's final text, so the test can assert the tool result reached it.

        Exposed as a query because the harness's update reply carries turn bookkeeping rather than
        the reply text, and this test deliberately does not wire the streaming handler.
        """
        return self._last_output

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the agent to search the workspace."""
        result = await _TEMPORAL_AGENT.run(
            message.text,
            deps=HarnessDeps(runner=self._runner),
        )
        self._last_output = str(result.output)
        return TextReply(text=self._last_output)


# ---------------------------------------------------------------------------
# Fixture + test
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client_and_queue() -> Any:
    # PydanticAIPlugin supplies the pydantic data converter and the sandbox passthroughs the SDK
    # needs; AgentPlugin registers the durable agent's own activities on the worker.
    env = await WorkflowEnvironment.start_time_skipping(plugins=[PydanticAIPlugin()])
    provider = SandboxProvider(PROVIDER, BACKEND)
    task_queue = f"pydantic-sandbox-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[PydanticSandboxProbeAgent],
        activities=list(provider.activities()),
        plugins=[AgentPlugin(_TEMPORAL_AGENT)],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _tool_events(client: Client, workflow_id: str) -> list[Any]:
    stream = WorkflowStreamClient.create(client, workflow_id)
    events: list[Any] = []
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC],
        from_offset=0,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=10),
    ):
        envelope: AgentEvent = item.data
        events.append(envelope.event)
        if envelope.event.type == AgentEventType.TURN_END:
            break
    return events


async def test_pydantic_ai_tool_receives_a_sandbox(client_and_queue: Any) -> None:
    """A Pydantic AI tool gets a working sandbox handle — no RunConfig, no OpenAI SDK."""
    client, task_queue = client_and_queue
    BACKEND.creates = 0
    BACKEND.hydrates = 0

    handle = await client.start_workflow(
        PydanticSandboxProbeAgent.run,
        AgentConfig(),
        id=f"PydanticSandboxProbe-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="ask", payload={"text": "find the needle"}, expected_turn=1),
        result_type=AgentMessageReply,
    )

    # The update returns turn bookkeeping and the turn proceeds asynchronously, so wait for the
    # stream to reach TURN_END before asserting on anything the turn produced.
    events = await _tool_events(client, handle.id)
    starts = [e for e in events if e.type == AgentEventType.TOOL_START]
    ends = [e for e in events if e.type == AgentEventType.TOOL_END]

    # The harness still owns the tool lifecycle, and the injected parameter never reaches the model:
    # the model chose only `pattern`, and `sandbox` was filled by the workflow.
    assert [s.tool_name for s in starts] == ["grep_workspace"]
    assert starts[0].tool_input == {"pattern": "needle"}

    # The tool's output could only exist if the handle really dispatched into the backend.
    assert "notes.md:2:the needle is here" in ends[0].tool_output

    # ...and the model saw that output and answered from it, so the full loop closed.
    assert "notes.md:2:the needle is here" in await handle.query(
        PydanticSandboxProbeAgent.last_output
    )

    # Claimed and hydrated exactly once for the run, lazily, on the tool's first touch.
    assert BACKEND.creates == 1
    assert BACKEND.hydrates == 1
