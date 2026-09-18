"""A real pre-integration history must replay without making provider calls."""

import gzip
from pathlib import Path

import pytest
from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from sdlc_builder.integrations import registered_integrations
from sdlc_builder.workflow import SdlcAgentWorkflow


@pytest.mark.parametrize(
    "filename,task_id",
    [
        ("legacy_plan_history.json.gz", "a" * 32),
        ("pre_conversation_review_history.json.gz", "f" * 32),
    ],
)
async def test_prior_task_histories_replay(filename, task_id):
    history = WorkflowHistory.from_json(
        "sdlc-" + task_id,
        gzip.decompress(
            (Path(__file__).parent / "fixtures" / filename).read_bytes()
        ).decode(),
    )
    assert "sdlc-conversation-v1" not in history.to_json()
    plugins = [
        plugin
        for backend in registered_integrations().values()
        for plugin in backend.client_plugins()
    ]
    replayer = Replayer(
        workflows=[SdlcAgentWorkflow], plugins=[*plugins, AgentHarnessPlugin()]
    )
    result = await replayer.replay_workflow(history)
    assert result.replay_failure is None
