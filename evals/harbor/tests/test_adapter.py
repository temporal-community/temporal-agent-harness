import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.models.agent.context import AgentContext
from temporal_agent_harness.harness.agent_protocol import AgentEvent

from harness_evals.adapter import TemporalHarnessAgent


@pytest.mark.parametrize("kwargs", [{}, {"workflow_type": "MyAgent"}, {"task_queue": "my-agent"}])
def test_agent_target_must_be_explicit(tmp_path, kwargs):
    with pytest.raises(ValueError):
        TemporalHarnessAgent(logs_dir=tmp_path, **kwargs)


def wire_client(monkeypatch, events, *, stall=False):
    handle = SimpleNamespace(
        id="owned-session", signal=AsyncMock(), result=AsyncMock(), terminate=AsyncMock()
    )
    temporal = SimpleNamespace(start_workflow=AsyncMock(return_value=handle))
    monkeypatch.setattr("harness_evals.adapter.Client.connect", AsyncMock(return_value=temporal))

    async def stream():
        for event in events:
            yield AgentEvent.model_validate(event)
        if stall:
            await asyncio.sleep(10)

    client = SimpleNamespace(
        submit_message=AsyncMock(
            return_value=SimpleNamespace(message_id="message1", turn_id="turn1")
        ),
        attach=AsyncMock(return_value=stream()),
    )
    monkeypatch.setattr("harness_evals.adapter.AgentClient", lambda *_: client)
    return temporal, handle


async def test_runs_fresh_session_and_closes_after_capture(tmp_path, monkeypatch, weather_record):
    temporal, handle = wire_client(monkeypatch, weather_record["events"])
    adapter = TemporalHarnessAgent(
        logs_dir=tmp_path, task_queue="example-evals", workflow_type="HelloAgent"
    )
    await adapter.setup(None)
    await adapter.run(weather_record["instruction"], None, AgentContext())
    record = json.loads((tmp_path / "run.json").read_text())
    assert record["complete"] is True
    assert record["output"] == weather_record["output"]
    assert record["session_id"].startswith("harness-eval-")
    assert temporal.start_workflow.call_args.kwargs["id"] == record["session_id"]
    handle.signal.assert_awaited_once_with("close")
    handle.terminate.assert_not_awaited()


async def test_timeout_preserves_partial_evidence_and_closes(tmp_path, monkeypatch, weather_record):
    _, handle = wire_client(monkeypatch, weather_record["events"][:4], stall=True)
    adapter = TemporalHarnessAgent(
        logs_dir=tmp_path,
        task_queue="example-evals",
        session_timeout_sec=0.05,
        workflow_type="HelloAgent",
    )
    with pytest.raises(TimeoutError):
        await adapter.run(weather_record["instruction"], None, AgentContext())
    record = json.loads((tmp_path / "run.json").read_text())
    assert record["complete"] is False
    assert record["error"] == "TimeoutError"
    assert (tmp_path / "trajectory.json").is_file()
    handle.signal.assert_awaited_once_with("close")


async def test_export_failure_still_closes_session(tmp_path, monkeypatch, weather_record):
    _, handle = wire_client(monkeypatch, weather_record["events"])
    monkeypatch.setattr(
        "harness_evals.adapter.export_atif",
        lambda _: (_ for _ in ()).throw(
            ValueError("bad evidence"),
        ),
    )
    adapter = TemporalHarnessAgent(
        logs_dir=tmp_path, task_queue="example-evals", workflow_type="HelloAgent"
    )
    with pytest.raises(ValueError, match="bad evidence"):
        await adapter.run(weather_record["instruction"], None, AgentContext())
    handle.signal.assert_awaited_once_with("close")
    assert (tmp_path / "run.json").is_file()


async def test_model_override_is_rejected_instead_of_silently_ignored(tmp_path):
    adapter = TemporalHarnessAgent(
        logs_dir=tmp_path,
        task_queue="example-evals",
        model_name="openai/another-model",
        workflow_type="HelloAgent",
    )
    with pytest.raises(ValueError, match="Configure the model on the agent worker"):
        await adapter.setup(None)


def test_targets_have_distinct_harbor_result_identity(tmp_path):
    a = TemporalHarnessAgent(
        logs_dir=tmp_path, task_queue="example-evals", workflow_type="OpenAIHelloAgent"
    )
    b = TemporalHarnessAgent(
        logs_dir=tmp_path, task_queue="example-evals", workflow_type="PydanticAIHelloAgent"
    )
    assert a.to_agent_info().name != b.to_agent_info().name
