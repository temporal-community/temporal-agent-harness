"""``TASK_QUEUE_PREFIX`` moves a deployment's workers, routing and session manager together."""

from __future__ import annotations

from pathlib import Path

import pytest

from temporal_agent_harness.utils.task_queue import task_queue_name
from temporal_agent_harness.web.registry import load_agent_registry

REGISTRY_TOML = """
[[agents]]
key = "react"
workflow_type = "ReactAgent"
task_queue = "react-agent"
label = "React"
description = "Reacts."
"""


def test_without_a_prefix_names_are_unchanged(monkeypatch) -> None:
    monkeypatch.delenv("TASK_QUEUE_PREFIX", raising=False)
    assert task_queue_name("react-agent") == "react-agent"


def test_a_prefix_is_put_in_front_of_the_name(monkeypatch) -> None:
    monkeypatch.setenv("TASK_QUEUE_PREFIX", "ws-42")
    assert task_queue_name("react-agent") == "ws-42-react-agent"


@pytest.mark.parametrize(
    ("prefix", "expected"), [(None, "react-agent"), ("ws-42", "ws-42-react-agent")]
)
def test_the_server_routes_to_the_queue_the_worker_polls(
    monkeypatch, tmp_path: Path, prefix: str | None, expected: str
) -> None:
    if prefix is None:
        monkeypatch.delenv("TASK_QUEUE_PREFIX", raising=False)
    else:
        monkeypatch.setenv("TASK_QUEUE_PREFIX", prefix)
    registry_path = tmp_path / "agents.toml"
    registry_path.write_text(REGISTRY_TOML)

    [agent] = load_agent_registry(registry_path).agents

    assert agent.task_queue == expected == task_queue_name("react-agent")
