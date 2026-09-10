# ABOUTME: Tests for AgentHarnessPlugin — the one plugin that wires a client + worker for the
# harness. Covers what it registers (subagent, Code Mode, and tool activities), what it
# refuses (a non-harness "tool"), how its data converter composes with an AI-SDK plugin's, and
# that it never double-registers an activity a worker already passed by hand.
#
# Lives in a subdirectory, not at the tests/ root: pytest prepends a test file's basedir to
# sys.path, and a file directly under tests/ would put `tests/` there — where the namespace
# directory tests/examples shadows the repo-root `examples` package the example tests import.
#
# Run with: `uv run pytest tests/plugin/test_plugin.py -v`

from __future__ import annotations

import sys
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import PydanticPayloadConverter, pydantic_data_converter
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import RUN_SUBAGENT_TURN_ACTIVITY
from temporal_agent_harness.harness.code_mode.batch_models import (
    CODE_RESUME_BATCH_ACTIVITY,
    CODE_START_BATCH_ACTIVITY,
)
from temporal_agent_harness.harness.code_mode.activities import (
    CODE_MODE_ACTIVITIES,
    CODE_MODE_MISSING_EXTRA_ERROR,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporal_agent_harness.utils.large_payload import (
    DEFAULT_PAYLOAD_STORAGE,
    local_payload_storage,
)

_CODE_MODE_NAMES = {CODE_START_BATCH_ACTIVITY, CODE_RESUME_BATCH_ACTIVITY}


@agent.activity_tool_defn(
    activity_config=ActivityConfig(start_to_close_timeout=timedelta(seconds=5)),
)
async def durable_tool(city: str) -> str:
    """A durable, activity-backed tool."""
    return city


@agent.tool_defn()
async def inline_tool(text: str) -> str:
    """An inline tool that runs in the workflow."""
    return text


@agent.callback_tool_defn()
async def callback_tool(path: str) -> str:
    """A tool fulfilled by an attached client."""
    ...


async def plain_function(x: int) -> int:
    return x


@workflow.defn(name="PluginCodeModeStepWorkflow")
class _CodeModeStepWorkflow:
    """Dispatches one Code Mode sandbox step exactly as ``CodeModeDriver`` does — by NAME.

    Standing in for a full Code Mode agent on purpose: the name-based dispatch IS the seam the
    plugin's registration has to satisfy, and everything else an agent brings (a runner, turns,
    approvals, the tool lifecycle) is unchanged and already covered by
    tests/examples/monty/test_code_mode_e2e.py. Note the deliberate absence of a retry policy:
    the default is unlimited attempts, so this workflow only fails fast if the error really is
    non-retryable.
    """

    @workflow.run
    async def run(self) -> object:
        return await workflow.execute_activity(
            CODE_START_BATCH_ACTIVITY,
            args=["1 + 1", None],
            start_to_close_timeout=timedelta(seconds=10),
        )


@workflow.defn(name="PluginTestWorkflow")
class _StubWorkflow:
    """A placeholder so ``Worker`` has something to host (it rejects an empty worker)."""

    @workflow.run
    async def run(self) -> None: ...


def _pretend_extra_missing(monkeypatch) -> None:
    """Make the engine unavailable exactly as it is on a worker without the extra.

    Binding the name to ``None`` in ``sys.modules`` is the documented way to make ``import
    pydantic_monty`` raise ImportError; monkeypatch undoes it afterwards. Uninstalling the
    extra for one test isn't an option on a dev machine that legitimately has it.

    Also drops any already-imported ``monty_stepper``, since that module imports Monty at
    module scope: without this, an earlier test's successful import would still be cached in
    ``sys.modules`` and the activity's local import would succeed anyway.
    """
    monkeypatch.setitem(sys.modules, "pydantic_monty", None)
    monkeypatch.delitem(
        sys.modules,
        "temporal_agent_harness.harness.code_mode.monty_stepper",
        raising=False,
    )


def _plugin_activity_names(plugin: AgentHarnessPlugin) -> set[str]:
    """The activity names a plugin contributes to a worker, before it sees one."""
    return {
        getattr(fn, "__temporal_activity_definition").name
        for fn in plugin._worker_activities
    }


def _registered_activity_names(worker: Worker) -> set[str]:
    """The activity names a built ``Worker`` will actually serve.

    ``active_config=True`` is the post-plugin view; the default returns the arguments the
    caller passed, which is exactly what the plugin is meant to shrink.
    """
    return {
        name
        for name in (
            getattr(
                getattr(fn, "__temporal_activity_definition", None), "name", None
            )
            for fn in worker.config(active_config=True)["activities"]
        )
        if name is not None
    }


# ---------------------------------------------------------------- data converter


def test_data_converter_fills_in_pydantic_and_offload():
    converter = AgentHarnessPlugin().data_converter(None)
    assert converter.payload_converter_class is PydanticPayloadConverter
    assert converter.external_storage is not None
    [driver] = converter.external_storage.drivers
    assert driver.name() == "local-file"


def test_data_converter_upgrades_a_default_converter_in_place():
    # A caller who passed `data_converter=DataConverter.default` still gets both requirements.
    converter = AgentHarnessPlugin().data_converter(DataConverter.default)
    assert converter.payload_converter_class is PydanticPayloadConverter
    assert converter.external_storage is not None


def test_data_converter_keeps_an_ai_sdk_payload_converter():
    """The plugin runs LAST, so an AI SDK's payload converter must survive it."""
    openai_agents = pytest.importorskip(
        "temporal_agent_harness.ai_sdks.openai_agents._temporal_openai_agents"
    )
    sdk_converter = DataConverter(
        payload_converter_class=openai_agents.OpenAIPayloadConverter
    )

    converter = AgentHarnessPlugin().data_converter(sdk_converter)

    assert converter.payload_converter_class is openai_agents.OpenAIPayloadConverter
    assert converter.external_storage is not None, "offload is still added"


def test_data_converter_keeps_an_existing_external_storage(tmp_path):
    import dataclasses

    mine = local_payload_storage(base_dir=tmp_path)
    converter = AgentHarnessPlugin().data_converter(
        dataclasses.replace(pydantic_data_converter, external_storage=mine)
    )
    assert converter.external_storage is mine


def test_the_offload_target_is_configuration_not_a_flag(tmp_path):
    """The caller hands the plugin an ``ExternalStorage``; the harness reads no env vars."""
    mine = local_payload_storage(base_dir=tmp_path, payload_size_threshold=64)
    converter = AgentHarnessPlugin(large_payload_offload=mine).data_converter(None)
    assert converter.external_storage is mine
    assert converter.external_storage.payload_size_threshold == 64


def test_the_default_offload_target_is_the_shared_local_storage():
    converter = AgentHarnessPlugin().data_converter(None)
    assert converter.external_storage is DEFAULT_PAYLOAD_STORAGE


def test_offload_can_be_turned_off():
    converter = AgentHarnessPlugin(large_payload_offload=None).data_converter(None)
    assert converter.payload_converter_class is PydanticPayloadConverter
    assert converter.external_storage is None


# ---------------------------------------------------------------- tools


def test_tools_registers_activity_bodies_and_skips_bodiless_tools():
    plugin = AgentHarnessPlugin(tools=[durable_tool, inline_tool, callback_tool])
    names = _plugin_activity_names(plugin)
    assert names - _CODE_MODE_NAMES == {"durable_tool"}


def test_tools_rejects_a_non_harness_tool():
    with pytest.raises(TypeError, match="not a harness tool"):
        AgentHarnessPlugin(tools=[plain_function])


# ---------------------------------------------------------------- code mode


def test_code_mode_activities_are_registered_unconditionally(monkeypatch):
    """Registration never branches on the extra — the check lives inside the activity.

    Registering the names is what matters: the driver dispatches Code Mode by name, and
    leaving them unregistered would make Temporal retry "not registered" forever.
    """
    assert set(AgentHarnessPlugin()._worker_activities) == set(CODE_MODE_ACTIVITIES)

    _pretend_extra_missing(monkeypatch)
    plugin = AgentHarnessPlugin()
    assert set(plugin._worker_activities) == set(CODE_MODE_ACTIVITIES)
    assert _plugin_activity_names(plugin) == _CODE_MODE_NAMES


async def test_a_code_mode_call_fails_actionably_without_the_extra(monkeypatch):
    """One immediate, actionable failure instead of a hung turn.

    Runs the REAL ``code_start_batch`` against an import that fails, asserting the developer
    is told what to install and that the failure is non-retryable — so the turn ends instead
    of retrying a missing dependency forever, which is what leaving the activity names
    unregistered would do.
    """
    _pretend_extra_missing(monkeypatch)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="plugin-no-code-mode",
            workflows=[_CodeModeStepWorkflow],
            plugins=[AgentHarnessPlugin()],
        ):
            with pytest.raises(WorkflowFailureError) as caught:
                await env.client.execute_workflow(
                    _CodeModeStepWorkflow.run,
                    id="plugin-no-code-mode-1",
                    task_queue="plugin-no-code-mode",
                )

    # ActivityError -> ApplicationError, non-retryable, naming the extra to install.
    cause = caught.value.cause.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == CODE_MODE_MISSING_EXTRA_ERROR
    assert cause.non_retryable
    assert "temporal-agent-harness[code-mode]" in str(cause)


# ---------------------------------------------------------------- worker wiring


async def test_worker_gets_every_harness_activity_from_the_plugin():
    """The headline claim: add the plugin, declare only workflows, get the capabilities."""
    pytest.importorskip("pydantic_monty")
    plugin = AgentHarnessPlugin(tools=[durable_tool, inline_tool])

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        worker = Worker(
            env.client, task_queue="plugin-test", workflows=[_StubWorkflow], plugins=[plugin]
        )
        assert _registered_activity_names(worker) == {
            "durable_tool",
            CODE_START_BATCH_ACTIVITY,
            CODE_RESUME_BATCH_ACTIVITY,
            RUN_SUBAGENT_TURN_ACTIVITY,
        }


async def test_hand_registered_harness_activities_are_not_duplicated():
    """A half-migrated worker that still passes the harness activities itself must start.

    Temporal rejects two activities with one name, so the plugin merges by name and lets the
    explicitly passed function win.
    """
    from temporal_agent_harness.harness.subagent_activities import SubagentActivities

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        hand_written = [
            *CODE_MODE_ACTIVITIES,
            SubagentActivities(env.client).run_subagent_turn,
            agent.tool_activity(durable_tool),
        ]
        worker = Worker(
            env.client,
            task_queue="plugin-test",
            workflows=[_StubWorkflow],
            activities=hand_written,
            plugins=[AgentHarnessPlugin(tools=[durable_tool])],
        )
        activities = worker.config(active_config=True)["activities"]
        assert len(activities) == len(hand_written)
        assert _registered_activity_names(worker) == {
            "durable_tool",
            CODE_START_BATCH_ACTIVITY,
            CODE_RESUME_BATCH_ACTIVITY,
            RUN_SUBAGENT_TURN_ACTIVITY,
        }


async def test_unrelated_worker_activities_survive():
    @activity.defn(name="unrelated")
    async def unrelated() -> None: ...

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        worker = Worker(
            env.client,
            task_queue="plugin-test",
            workflows=[_StubWorkflow],
            activities=[unrelated],
            plugins=[AgentHarnessPlugin()],
        )
        assert _registered_activity_names(worker) == {
            "unrelated",
            RUN_SUBAGENT_TURN_ACTIVITY,
            *_CODE_MODE_NAMES,
        }


# ---------------------------------------------------------------- end to end


@workflow.defn(name="PluginBigPayloadWorkflow")
class _BigPayloadWorkflow:
    """Returns a payload well past Temporal's ~2 MB limit, so it must be offloaded."""

    @workflow.run
    async def run(self, size: int) -> str:
        return "z" * size


async def test_client_registered_plugin_reaches_the_worker():
    """Every example registers the plugin on the CLIENT only — Temporal must carry it through.

    A client's plugins are applied to the workers built from that client, which is what lets an
    agent worker declare nothing but its workflows.
    """
    async with await WorkflowEnvironment.start_time_skipping(
        plugins=[AgentHarnessPlugin(tools=[durable_tool])]
    ) as env:
        worker = Worker(env.client, task_queue="plugin-test", workflows=[_StubWorkflow])
        assert _registered_activity_names(worker) == {
            "durable_tool",
            RUN_SUBAGENT_TURN_ACTIVITY,
            *_CODE_MODE_NAMES,
        }


async def test_plugins_configured_storage_offloads_a_large_payload(tmp_path):
    """The storage the caller passes has to work inside a real, connected client.

    Exercises the whole path end to end — plugin config → data converter → an oversized
    workflow result — and checks the payload really left the event history and landed in the
    directory the test chose (no env var in sight).
    """
    storage = local_payload_storage(base_dir=tmp_path)

    size = 2_000_000
    async with await WorkflowEnvironment.start_time_skipping(
        plugins=[AgentHarnessPlugin(large_payload_offload=storage)]
    ) as env:
        async with Worker(
            env.client, task_queue="plugin-offload", workflows=[_BigPayloadWorkflow]
        ):
            result = await env.client.execute_workflow(
                _BigPayloadWorkflow.run,
                size,
                id="plugin-offload-1",
                task_queue="plugin-offload",
            )
    assert result == "z" * size
    assert list(tmp_path.glob("*.bin")), "the payload should have been offloaded"
