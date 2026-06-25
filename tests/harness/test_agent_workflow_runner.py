# ABOUTME: Tests for the AgentWorkflowRunner handler-dispatch model — that @agent.accepts
# handlers are discovered and validated, that an inbound send_agent_message envelope routes
# by `type` to the matching handler (reconstructing its input model, rejecting an unknown
# function or a malformed payload at the update boundary), that the handler's return value
# is published as the reply, that the agent_interface query announces the callable surface,
# and that the runner resolves config-vs-agent-default knobs (with stream + approval policy
# required).
#
# The accept/reject/dispatch behavior is exercised end-to-end through real updates against
# the Temporal time-skipping test server (the only faithful way — routing lives in the
# update validator + run loop, which run in a workflow context). The discovery/validation
# and config-resolution checks are plain unit tests.
#
# Run with: uv run pytest tests/harness/test_agent_workflow_runner.py -v

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import Client, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    AGENT_INTERFACE_QUERY,
    AGENT_STATUS_QUERY,
    EXECUTE_OPERATOR_COMMAND_UPDATE,
    OPERATOR_INTERFACE_QUERY,
    SEND_AGENT_MESSAGE_UPDATE,
    AcceptedFunction,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentStatus,
    OperatorCommand,
    OperatorCommandArgument,
    OperatorCommandRequest,
    OperatorCommandResult,
    SubagentReplyReceived,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
    AgentMessageReply,
    SlashCommand,
)
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_workflow import _discover_handlers

# ---------------------------------------------------------------------------
# Message models + probe workflows
# ---------------------------------------------------------------------------


class Greeting(BaseModel):
    """A person to greet."""

    name: str


class Greeted(BaseModel):
    """The greeting produced for a person."""

    message: str


class ModelPick(BaseModel):
    """A model selection."""

    model: str


class Picked(BaseModel):
    """The confirmed model selection."""

    model: str


PROBE_MODEL_OPERATOR_COMMAND = OperatorCommand(
    name="model",
    payload_name="set-model",
    label="/model",
    description="Set the probe model.",
    argument=OperatorCommandArgument(
        kind="enum",
        choices=("alpha", "beta"),
        placeholder="model",
    ),
    source="agent",
)


@workflow.defn
@agent.defn
class TypedProbeAgent:
    """Two handlers — greet(Greeting)->Greeted and pick(ModelPick)->Picked — plus a
    failing handler. Records each handled message so a test can confirm the runner routed
    + reconstructed the concrete input model (not a dict)."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Default queuing on (tests send several messages back-to-back); a config
            # value would still win over this default.
            enable_message_queuing_default=True,
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._seen: list[str] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def greet(self, message: Greeting) -> Greeted:
        """Greet a person by name."""
        self._seen.append(f"greet:{message.name}")
        return Greeted(message=f"hi {message.name}")

    @agent.accepts
    async def pick(self, message: ModelPick) -> Picked:
        """Pick a model for the session."""
        self._seen.append(f"pick:{message.model}")
        return Picked(model=message.model)

    @agent.accepts
    async def boom(self, message: TextMessage) -> TextReply:
        """Always raises — to prove an errored turn publishes AgentError + turn_end and
        the loop survives for the next message."""
        raise RuntimeError(f"boom: {message.text}")

    @workflow.query
    def seen(self) -> list[str]:
        return self._seen


@workflow.defn
@agent.defn
class SlashExtensionProbeAgent:
    """Agent with an agent-specific slash extension, used to prove harness slash commands
    are handled first and unknown commands still fall through to the agent."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
            operator_commands=[PROBE_MODEL_OPERATOR_COMMAND],
            operator_command_handler=self._handle_operator_command,
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def slash(self, command: SlashCommand) -> TextReply:
        """Handle agent-specific slash commands."""
        return TextReply(text=f"custom:{command.name}:{command.arg or ''}")

    def _handle_operator_command(self, command: SlashCommand) -> TextReply | None:
        if command.name == "set-model":
            return TextReply(text=f"operator:{command.name}:{command.arg or ''}")
        return None


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client_and_queue():
    """A time-skipping env (pydantic converter) with a worker hosting the probe."""
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"agent-workflow-runner-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[TypedProbeAgent, SlashExtensionProbeAgent],
        # Unsandboxed so the test module's imports (pydantic, harness, pytest) don't
        # trip the workflow sandbox; the runner logic under test is unaffected.
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _start(client: Client, task_queue: str, wf: Any) -> WorkflowHandle:
    return await client.start_workflow(
        wf.run, AgentConfig(), id=f"{wf.__name__}-{uuid.uuid4()}", task_queue=task_queue
    )


async def _next_expected_turn(handle: WorkflowHandle) -> int:
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    return status.current_turn + len(status.pending_turns) + 1


async def _send(
    handle: WorkflowHandle, type: str, payload: dict[str, Any]
) -> AgentMessageReply:
    return await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(
            type=type,
            payload=payload,
            expected_turn=await _next_expected_turn(handle),
        ),
        result_type=AgentMessageReply,
    )


async def _operator(
    handle: WorkflowHandle, name: str, arg: str | None = None
) -> OperatorCommandResult:
    return await handle.execute_update(
        EXECUTE_OPERATOR_COMMAND_UPDATE,
        OperatorCommandRequest(name=name, arg=arg),
        result_type=OperatorCommandResult,
    )


async def _wait_for_seen(
    handle: WorkflowHandle, count: int, *, attempts: int = 200, delay: float = 0.05
) -> list[str]:
    seen: list[str] = []
    for _ in range(attempts):
        seen = await handle.query("seen", result_type=list[str])
        if len(seen) >= count:
            return seen
        await asyncio.sleep(delay)
    raise AssertionError(f"timed out waiting for {count} seen entries; got {seen}")


# ---------------------------------------------------------------------------
# Routing + dispatch (end-to-end)
# ---------------------------------------------------------------------------


async def test_routes_by_type_and_reconstructs_input(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    await _send(handle, "greet", {"name": "Ada"})
    await _send(handle, "pick", {"model": "opus"})

    seen = await _wait_for_seen(handle, 2)
    # FIFO order, and each message arrived at its handler as the concrete input model.
    assert seen == ["greet:Ada", "pick:opus"]


async def test_rejects_unknown_function(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    with pytest.raises(WorkflowUpdateFailedError) as excinfo:
        await _send(handle, "does_not_exist", {})
    cause = excinfo.value.cause
    assert getattr(cause, "type", None) == "UnknownFunction"
    # The rejection spells out the known functions so a caller can self-correct.
    detail = str(cause)
    assert "greet" in detail and "pick" in detail

    # The rejected message created no turn.
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.current_turn == 0 and status.pending_turns == []


async def test_rejects_malformed_payload(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    # `greet` requires {name: str}; an empty payload fails its input model.
    with pytest.raises(WorkflowUpdateFailedError) as excinfo:
        await _send(handle, "greet", {})
    cause = excinfo.value.cause
    assert getattr(cause, "type", None) == "MalformedMessage"
    assert "Greeting" in str(cause)


async def test_agent_interface_query_announces_handlers(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    functions = await handle.query(
        AGENT_INTERFACE_QUERY, result_type=list[AcceptedFunction]
    )
    by_name = {f.name: f for f in functions}
    assert set(by_name) == {"greet", "pick", "boom"}
    # Description is the handler docstring; parameters/output are the model schemas.
    assert by_name["greet"].description == "Greet a person by name."
    assert "name" in by_name["greet"].parameters["properties"]
    assert "message" in by_name["greet"].output["properties"]


async def test_agent_interface_hides_operator_slash_handler(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, SlashExtensionProbeAgent)

    functions = await handle.query(
        AGENT_INTERFACE_QUERY, result_type=list[AcceptedFunction]
    )

    assert {f.name for f in functions} == set()


async def test_operator_interface_lists_harness_commands_for_every_agent(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    commands = await handle.query(
        OPERATOR_INTERFACE_QUERY, result_type=list[OperatorCommand]
    )
    by_name = {command.name: command for command in commands}

    assert set(by_name) == {"approvals", "allow-tools", "status"}
    assert by_name["approvals"].source == "harness"
    assert by_name["approvals"].payload_name == "set-approvals"
    assert by_name["approvals"].argument is not None
    assert by_name["approvals"].argument.kind == "enum"
    assert by_name["approvals"].argument.choices == ("strict", "safe", "skip")
    assert by_name["allow-tools"].argument is not None
    assert by_name["allow-tools"].argument.kind == "tool_names"
    assert "allow-tool" in by_name["allow-tools"].aliases


async def test_operator_interface_includes_agent_extension_commands(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, SlashExtensionProbeAgent)

    commands = await handle.query(
        OPERATOR_INTERFACE_QUERY, result_type=list[OperatorCommand]
    )
    by_name = {command.name: command for command in commands}

    assert set(by_name) == {"approvals", "allow-tools", "status", "model"}
    assert by_name["model"].source == "agent"
    assert by_name["model"].payload_name == "set-model"
    assert by_name["model"].argument is not None
    assert by_name["model"].argument.choices == ("alpha", "beta")


async def test_operator_command_status_does_not_create_turn(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    result = await _operator(handle, "status")

    assert "- Agent id:" in result.text
    assert "- Approvals: `skip`" in result.text
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.current_turn == 0
    assert status.pending_turns == []


async def test_operator_command_set_approvals_updates_policy_without_turn(
    client_and_queue,
):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    result = await _operator(handle, "set-approvals", "safe")

    assert result.text == "Approvals set to **safe**."
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.approval_policy == ToolApprovalPolicy.allow_inherently_safe()
    assert status.current_turn == 0


async def test_operator_command_allow_tools_updates_policy_without_turn(
    client_and_queue,
):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    result = await _operator(handle, "allow-tools", "alpha_tool,beta_tool")

    assert result.text == "Tools `alpha_tool`, `beta_tool` will be auto-approved."
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.approval_policy.auto_approve_tools == frozenset(
        {"alpha_tool", "beta_tool"}
    )
    assert status.current_turn == 0


async def test_operator_command_uses_agent_extension_callback(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, SlashExtensionProbeAgent)

    result = await _operator(handle, "set-model", "alpha")

    assert result.text == "operator:set-model:alpha"
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.current_turn == 0


async def test_operator_command_preempts_agent_extension_for_core_commands(
    client_and_queue,
):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, SlashExtensionProbeAgent)

    result = await _operator(handle, "status")

    assert "- Agent id:" in result.text
    assert "operator:status" not in result.text


async def _collect_until_turn_end(client: Client, workflow_id: str) -> list[AgentEvent]:
    from datetime import timedelta

    from temporalio.contrib.workflow_streams import WorkflowStreamClient

    stream = WorkflowStreamClient.create(client, workflow_id)
    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in stream.subscribe(
            topics=["turn_events"],
            from_offset=0,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            events.append(item.data)
            if item.data.event.type == AgentEventType.TURN_END:
                break
    return events


def _reply_text(events: list[AgentEvent]) -> str:
    reply = next(e.event for e in events if e.event.type == AgentEventType.REPLY)
    text = reply.output.get("text")
    assert isinstance(text, str)
    return text


async def test_reply_is_the_handler_return_value(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)
    await _send(handle, "greet", {"name": "Ada"})

    events = await _collect_until_turn_end(client, handle.id)
    reply = next(e.event for e in events if e.event.type == AgentEventType.REPLY)
    # The reply carries the handler's return model serialized to a dict.
    assert reply.output == {"message": "hi Ada"}


async def test_harness_slash_status_without_agent_handler(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    await _send(handle, "slash", {"name": "status"})

    text = _reply_text(await _collect_until_turn_end(client, handle.id))
    assert "- Agent id:" in text
    assert "- Approvals: `skip`" in text
    assert "- Pending approvals: none" in text


async def test_harness_slash_set_approvals_updates_policy(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    await _send(handle, "slash", {"name": "set-approvals", "arg": "safe"})

    text = _reply_text(await _collect_until_turn_end(client, handle.id))
    assert text == "Approvals set to **safe**."
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.approval_policy == ToolApprovalPolicy.allow_inherently_safe()


async def test_harness_slash_allow_tools_updates_allow_list(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    await _send(handle, "slash", {"name": "allow-tools", "arg": "alpha_tool,beta_tool"})

    text = _reply_text(await _collect_until_turn_end(client, handle.id))
    assert "Tools `alpha_tool`, `beta_tool` will be auto-approved." == text
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.approval_policy.auto_approve_tools == frozenset(
        {"alpha_tool", "beta_tool"}
    )


async def test_harness_slash_unknown_without_agent_handler_returns_reply(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    await _send(handle, "slash", {"name": "not-real"})

    text = _reply_text(await _collect_until_turn_end(client, handle.id))
    assert text == "Unknown slash command: `not-real`."


async def test_harness_slash_falls_back_to_agent_extension(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, SlashExtensionProbeAgent)

    await _send(handle, "slash", {"name": "set-model", "arg": "gemini"})

    text = _reply_text(await _collect_until_turn_end(client, handle.id))
    assert text == "custom:set-model:gemini"


async def test_harness_slash_preempts_agent_extension_for_core_commands(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, SlashExtensionProbeAgent)

    await _send(handle, "slash", {"name": "status"})

    text = _reply_text(await _collect_until_turn_end(client, handle.id))
    assert "- Agent id:" in text
    assert "custom:status" not in text


async def test_handler_error_publishes_agent_error_and_loop_survives(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    # A raising handler → AgentError (then turn_end), and the session stays alive.
    await _send(handle, "boom", {"text": "x"})
    events = await _collect_until_turn_end(client, handle.id)
    errors = [e for e in events if e.event.type == AgentEventType.ERROR]
    assert len(errors) == 1 and "boom: x" in errors[0].event.message
    # The next message is still handled normally.
    await _send(handle, "greet", {"name": "Bob"})
    seen = await _wait_for_seen(handle, 1)
    assert "greet:Bob" in seen


# ---------------------------------------------------------------------------
# Handler discovery + validation (pure unit tests)
# ---------------------------------------------------------------------------


def test_discovers_accepts_handlers():
    handlers = _discover_handlers(TypedProbeAgent)
    assert set(handlers) == {"greet", "pick", "boom"}
    assert handlers["greet"].input_type is Greeting
    assert handlers["greet"].output_type is Greeted


def test_discover_rejects_non_pydantic_input():
    class Bad:
        @agent.accepts
        async def h(self, message: int) -> Greeted:  # input not a pydantic model
            """h."""
            ...

    with pytest.raises(TypeError, match="must be annotated with a pydantic model"):
        _discover_handlers(Bad)


def test_discover_rejects_scalar_return():
    class Bad:
        @agent.accepts
        async def h(self, message: Greeting) -> str:  # scalar return
            """h."""
            ...

    with pytest.raises(TypeError, match="return type must be a pydantic model"):
        _discover_handlers(Bad)


def test_discover_rejects_missing_docstring():
    class Bad:
        @agent.accepts
        async def h(self, message: Greeting) -> Greeted:
            ...  # no docstring

    with pytest.raises(TypeError, match="must have a docstring"):
        _discover_handlers(Bad)


def test_discover_rejects_wrong_arity():
    class Bad:
        @agent.accepts
        async def h(self, a: Greeting, b: Greeting) -> Greeted:  # two args
            """h."""
            ...

    with pytest.raises(TypeError, match="exactly one argument"):
        _discover_handlers(Bad)


# ---------------------------------------------------------------------------
# @agent.defn signature contract
# ---------------------------------------------------------------------------


class _ValidAgentShape:
    @workflow.run
    async def run(self, config: AgentConfig) -> None: ...


class _MissingConfigShape:
    @workflow.run
    async def run(self) -> None: ...


class _WrongInputShape:
    @workflow.run
    async def run(self, value: int) -> None: ...


def test_agent_defn_accepts_single_agentconfig():
    assert agent.defn(_ValidAgentShape) is _ValidAgentShape


def test_agent_defn_rejects_missing_config_at_definition_time():
    with pytest.raises(TypeError, match="must accept exactly one AgentConfig"):
        agent.defn(_MissingConfigShape)


def test_agent_defn_rejects_bespoke_input_at_definition_time():
    with pytest.raises(TypeError, match="must accept exactly one AgentConfig"):
        agent.defn(_WrongInputShape)


# ---------------------------------------------------------------------------
# Direct construction is blocked
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Config resolution + required values (offline unit tests)
# ---------------------------------------------------------------------------


def test_stream_and_approval_policy_default_are_required():
    """``stream`` and ``approval_policy_default`` are required keyword-only constructor
    args, so omitting either is a call-site TypeError — no runtime ``build()`` check to
    forget. The author must make a deliberate safe-by-default approval choice."""
    stream = MagicMock()
    stream.topic.return_value = MagicMock()
    with pytest.raises(TypeError):
        AgentWorkflowRunner(  # type: ignore[call-arg]  — missing stream
            AgentConfig(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
    with pytest.raises(TypeError):
        AgentWorkflowRunner(  # type: ignore[call-arg]  — missing approval_policy_default
            AgentConfig(),
            stream=stream,
        )


def test_message_queuing_resolves_config_over_agent_default(offline_build):
    assert offline_build(AgentConfig())._status.is_message_queuing_enabled is False
    assert (
        offline_build(AgentConfig(), default=True)._status.is_message_queuing_enabled
        is True
    )
    assert (
        offline_build(
            AgentConfig(is_message_queuing_enabled=True), default=False
        )._status.is_message_queuing_enabled
        is True
    )
    assert (
        offline_build(
            AgentConfig(is_message_queuing_enabled=False), default=True
        )._status.is_message_queuing_enabled
        is False
    )


def test_approval_policy_resolves_config_over_agent_default(offline_build_policy):
    agent_default = ToolApprovalPolicy.allow_inherently_safe()
    caller_policy = ToolApprovalPolicy.dangerously_skip_all()
    assert (
        offline_build_policy(AgentConfig(), default=agent_default).current_approval_policy
        == agent_default
    )
    assert (
        offline_build_policy(
            AgentConfig(approval_policy=caller_policy), default=agent_default
        ).current_approval_policy
        == caller_policy
    )


def test_set_approval_policy_resolves_matching_pending(offline_build_policy):
    from temporal_agent_harness.harness.agent_workflow import _ApprovalStatus

    runner = offline_build_policy(
        AgentConfig(), default=ToolApprovalPolicy.always_require_approvals()
    )
    runner._status.register_pending_approval(
        "t1", "trusted_tool", {"x": 1}, 1, "turn-1", inherently_safe=False
    )
    runner._status.register_pending_approval(
        "t2", "other_tool", {}, 1, "turn-1", inherently_safe=False
    )

    runner.set_approval_policy(ToolApprovalPolicy.allow_tools(["trusted_tool"]))

    assert runner._status.is_approval_resolved("t1") is True
    entry = runner._status.approval_entry("t1")
    assert entry.status is _ApprovalStatus.APPROVED
    assert entry.reason == "auto-approved by updated policy"
    assert runner._status.is_approval_resolved("t2") is False


def test_custom_fallback_is_consulted_only_as_last_layer(offline_build_policy):
    calls: list[str] = []

    def fallback(ctx) -> bool:
        calls.append(ctx.tool_name)
        return ctx.tool_name == "blessed"

    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.dangerously_skip_all(),
        custom_fallback=fallback,
    )
    assert runner._auto_approves("anything", {}, inherently_safe=False) is True
    assert calls == []

    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.always_require_approvals(),
        custom_fallback=fallback,
    )
    assert runner._auto_approves("blessed", {}, inherently_safe=False) is True
    assert runner._auto_approves("cursed", {}, inherently_safe=False) is False
    assert calls == ["blessed", "cursed"]


def test_protocol_types_use_concrete_annotations():
    """Guard: the wire types must use concrete (not stringized) annotations — they cross
    the Temporal pydantic converter, which builds their TypeAdapter inside the workflow
    sandbox, where a stringized annotation fails to resolve."""
    from temporal_agent_harness.harness.agent_protocol import (
        AgentMessage,
        AgentMessageReply,
        AgentStatus,
        OperatorCommand,
        OperatorCommandArgument,
        OperatorCommandRequest,
        OperatorCommandResult,
        SlashCommand,
    )

    for cls in (
        AgentMessage,
        AgentStatus,
        AgentMessageReply,
        SlashCommand,
        OperatorCommand,
        OperatorCommandArgument,
        OperatorCommandRequest,
        OperatorCommandResult,
    ):
        for field_name, annotation in cls.__annotations__.items():
            assert not isinstance(annotation, str), (
                f"{cls.__name__}.{field_name} is a string annotation — "
                f"agent_interface.py must not use `from __future__ import annotations`."
            )


def test_errored_subagent_turn_closes_bracket_on_actual_accepted_turn(offline_build):
    """On an accepted-but-errored child turn, the parent closes the
    [subagent_message_sent … subagent_reply_received] bracket on the child's ACTUAL accepted turn
    number — which the activity threads through the error details — not a re-derived ``expected``.

    Keeps the close-gate key (``workflow_id``, ``subagent_turn``) matching the open marker by
    construction, independent of the validator+enqueue invariant that makes them equal in practice.
    """
    runner = offline_build(AgentConfig())
    # Make a turn active so publish() has a stream context to publish against.
    runner._status.enqueue_message(
        AgentMessage(type="x", payload={}, expected_turn=1), "turn-1"
    )
    runner._status.start_next_turn()
    inst = runner._status.register_subagent("aaaaaa-bbbbbb", "child-wf-1", "k")

    # The activity raises with the child's ACTUAL accepted turn number (7) in the details —
    # deliberately different from the ``expected``/default we pass (2), so the assertion proves we
    # use the threaded value and not ``expected``.
    err = ApplicationError(
        "subagent turn failed",
        {"subagent_turn": 7},
        type="SubagentTurnError",
        non_retryable=True,
    )
    accepted = runner._accepted_turn_from_error(err, default=2)
    assert accepted == 7
    runner._publish_subagent_reply_received(
        inst, "run_script", accepted, outcome="error"
    )

    published = [c.args[0] for c in runner._events.publish.call_args_list]
    replies = [e for e in published if isinstance(e.event, SubagentReplyReceived)]
    assert len(replies) == 1
    rr = replies[0].event
    assert rr.subagent_turn == 7  # the actual accepted turn, NOT the (wrong) expected=2
    assert rr.outcome == "error"
    assert rr.workflow_id == "child-wf-1"
    assert rr.subagent_id == "aaaaaa-bbbbbb"
    # The local turn counter advances off the same accepted turn.
    assert accepted + 1 == 8


def test_accepted_turn_from_error_falls_back_when_detail_absent():
    """If an error carries no ``subagent_turn`` detail (older activity build / unexpected shape),
    the parent falls back to the supplied ``default`` (``expected``) rather than failing."""
    err = ApplicationError("no reply", type="SubagentNoReply", non_retryable=True)
    assert AgentWorkflowRunner._accepted_turn_from_error(err, default=3) == 3


# ---------------------------------------------------------------------------
# Offline build fixtures (workflow APIs __init__ touches are patched out)
# ---------------------------------------------------------------------------


@pytest.fixture
def offline_build(monkeypatch):
    import temporal_agent_harness.harness.agent_workflow as aw

    for handler in ("set_update_handler", "set_query_handler", "set_signal_handler"):
        monkeypatch.setattr(aw.workflow, handler, lambda *a, **k: None)
    monkeypatch.setattr(aw.workflow, "time", lambda: 0.0)
    # The runner generates its short agent_id from workflow.uuid4() in __init__; offline there is
    # no workflow loop, so stub it with a plain uuid.
    monkeypatch.setattr(aw.workflow, "uuid4", lambda: uuid.uuid4())

    def build(config: AgentConfig, *, default: bool | None = None):
        stream = MagicMock()
        stream.topic.return_value = MagicMock()
        kwargs: dict[str, Any] = {}
        if default is not None:
            kwargs["enable_message_queuing_default"] = default
        return AgentWorkflowRunner(
            config,
            stream=stream,
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            **kwargs,
        )

    return build


@pytest.fixture
def offline_build_policy(monkeypatch):
    import temporal_agent_harness.harness.agent_workflow as aw

    for handler in ("set_update_handler", "set_query_handler", "set_signal_handler"):
        monkeypatch.setattr(aw.workflow, handler, lambda *a, **k: None)
    monkeypatch.setattr(aw.workflow, "time", lambda: 0.0)
    # The runner generates its short agent_id from workflow.uuid4() in __init__; offline there is
    # no workflow loop, so stub it with a plain uuid.
    monkeypatch.setattr(aw.workflow, "uuid4", lambda: uuid.uuid4())

    def build(config: AgentConfig, *, default: ToolApprovalPolicy, custom_fallback=None):
        stream = MagicMock()
        stream.topic.return_value = MagicMock()
        return AgentWorkflowRunner(
            config,
            stream=stream,
            approval_policy_default=default,
            custom_approval_fallback=custom_fallback,
        )

    return build
