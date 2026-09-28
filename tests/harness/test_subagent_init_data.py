# ABOUTME: Tests for a subagent's init data — that subagent_toolset's start_<key> tool gives each
# new child instance the `data` its @agent.init takes from the developer's init_data callback (or
# none without one), never takes the data from the parent model, and requires the callback for a
# child whose `data` is required.
#
# Run with: uv run pytest tests/harness/test_subagent_init_data.py -v

from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest
import pytest_asyncio
from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    AGENT_STATUS_QUERY,
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentMessage,
    AgentMessageReply,
    AgentStatus,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)


class Trip(BaseModel):
    """The trip a planner instance is dedicated to."""

    traveler: str


@agent.defn(name="TripPlannerChild")
class TripPlannerChild:
    @agent.init
    def __init__(self, config: AgentConfig, data: Trip | None = None) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._trip = data

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Answer a question about the trip."""
        return TextReply(text=message.text)

    @workflow.query
    def traveler(self) -> str | None:
        return self._trip.traveler if self._trip is not None else None


@agent.defn(name="RequiredTripPlannerChild")
class RequiredTripPlannerChild:
    @agent.init
    def __init__(self, config: AgentConfig, data: Trip) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._trip = data

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Answer a question about the trip."""
        return TextReply(text=message.text)

    @workflow.query
    def traveler(self) -> str | None:
        return self._trip.traveler


@agent.defn(name="UntypedChild")
class UntypedChild:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Echo the question."""
        return TextReply(text=message.text)


def _start_tool(cls: type, **kwargs):
    tools = agent.subagent_toolset(cls, key="planner", task_queue="q", **kwargs)
    return {t.__name__: t for t in tools}["start_planner"]


@pytest.mark.parametrize(
    ("cls", "kwargs"),
    [
        (TripPlannerChild, {}),
        (TripPlannerChild, {"init_data": lambda: Trip(traveler="Ada")}),
        (RequiredTripPlannerChild, {"init_data": lambda: Trip(traveler="Ada")}),
        (UntypedChild, {}),
    ],
)
def test_the_start_tool_never_takes_init_data_from_the_model(cls, kwargs):
    assert list(inspect.signature(_start_tool(cls, **kwargs)).parameters) == []


def test_init_data_is_rejected_for_a_child_that_declares_none():
    with pytest.raises(TypeError, match="takes no init data"):
        _start_tool(UntypedChild, init_data=lambda: Trip(traveler="x"))


def test_a_child_that_requires_init_data_requires_the_callback():
    with pytest.raises(TypeError, match="needs an init_data callback"):
        agent.subagent_toolset(RequiredTripPlannerChild, key="planner", task_queue="q")


class StartPlanner(BaseModel):
    """Start a planner through the generated start tool."""

    task_queue: str
    traveler: str


@agent.defn(name="PlannerParent")
class PlannerParent:
    """Starts planner children through the generated start tool."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._next_traveler = ""

    def _trip_for_next_instance(self) -> Trip:
        return Trip(traveler=self._next_traveler)

    async def _start(self, start_tool) -> TextReply:
        return TextReply(text=await self._runner.run_tool("start", start_tool))

    @agent.accepts
    async def start_from_callback(self, message: StartPlanner) -> TextReply:
        """Start a required-data planner whose data the parent derives."""
        start = {
            t.__name__: t
            for t in agent.subagent_toolset(
                RequiredTripPlannerChild,
                key="planner",
                task_queue=message.task_queue,
                init_data=self._trip_for_next_instance,
            )
        }["start_planner"]
        # The callback reads the parent's state at call time, not at toolset construction.
        self._next_traveler = message.traveler
        return await self._start(start)

    @agent.accepts
    async def start_without_callback(self, message: StartPlanner) -> TextReply:
        """Start an optional-data planner with no data."""
        start = {
            t.__name__: t
            for t in agent.subagent_toolset(
                TripPlannerChild, key="planner", task_queue=message.task_queue
            )
        }["start_planner"]
        return await self._start(start)


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"subagent-init-data-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[PlannerParent, TripPlannerChild, RequiredTripPlannerChild],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _child_traveler(client: Client, parent: WorkflowHandle) -> str | None:
    for _ in range(200):
        status = await parent.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
        if status.subagents:
            child = client.get_workflow_handle(status.subagents[0].workflow_id)
            return await child.query("traveler")
        await asyncio.sleep(0.05)
    raise AssertionError("the parent never started a subagent")


@pytest.mark.parametrize(
    ("handler", "expected"),
    [("start_from_callback", "Ada"), ("start_without_callback", None)],
)
async def test_the_child_gets_the_callbacks_data_or_none(client_and_queue, handler, expected):
    client, task_queue = client_and_queue
    parent = await client.start_workflow(
        PlannerParent.run, AgentConfig(), id=f"parent-{uuid.uuid4()}", task_queue=task_queue
    )
    await parent.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type=handler, payload={"task_queue": task_queue, "traveler": "Ada"}),
        result_type=AgentMessageReply,
    )
    assert await _child_traveler(client, parent) == expected


async def test_starting_a_required_data_agent_without_data_fails_the_workflow(
    client_and_queue,
):
    """Not a hang: the missing data fails the workflow at input decode instead of failing its
    first task on every retry."""
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        "RequiredTripPlannerChild",
        AgentConfig(),
        id=f"required-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    with pytest.raises(WorkflowFailureError) as excinfo:
        await asyncio.wait_for(handle.result(), timeout=30)
    assert "requires init data" in str(excinfo.value.cause)
