# ABOUTME: What the goldens test and the update program share: where scenarios and goldens live,
# recording every scenario against one scripted worker, and the scripted integration check that
# runs before any golden comparison.

from __future__ import annotations

import os
import shlex
from collections.abc import Sequence
from pathlib import Path

from temporal_agent_harness.harness.agent_protocol import AgentEventType
from tests.conformance.driver import (
    Recording,
    external_worker,
    load_scenario,
    record,
    scripted_worker,
)
from tests.conformance.script import INTEGRATION_EVENT_TYPES, Script, integration_events

# Relative to the repo root, where both the test (its runfiles root) and `bazel run` start.
SCENARIOS = Path("conformance/scenarios")
GOLDENS = Path("conformance/goldens")
UPDATE_COMMAND = "bazel run //python/tests/conformance:goldens_update"

WORKER_ENV = "CONFORMANCE_WORKER"
"""Names another SDK's worker program (see ``external_worker``) for the goldens test to record
against instead of Python's worker; a relative path is resolved against the working directory."""


def external_worker_command() -> list[str] | None:
    """The worker program ``WORKER_ENV`` names, made absolute, or None to use Python's worker."""
    command = shlex.split(os.environ.get(WORKER_ENV, ""))
    if not command:
        return None
    return [os.path.abspath(command[0]), *command[1:]]


def scenario_names() -> list[str]:
    return sorted(path.stem for path in SCENARIOS.glob("*.json"))


def events_golden(name: str) -> Path:
    return GOLDENS / f"{name}.events.jsonl"


def sends_golden(name: str) -> Path:
    return GOLDENS / f"{name}.sends.jsonl"


async def record_all(worker_command: Sequence[str] | None = None) -> dict[str, Recording]:
    """Every scenario, recorded against Python's worker or the program ``worker_command`` runs."""
    recordings: dict[str, Recording] = {}
    worker = scripted_worker() if worker_command is None else external_worker(worker_command)
    async with worker as (client, task_queue):
        for name in scenario_names():
            try:
                recordings[name] = await record(
                    client, task_queue, load_scenario(SCENARIOS / f"{name}.json")
                )
            except Exception as e:
                e.add_note(f"recording scenario {name}")
                raise
    return recordings


def integration_mismatches(recording: Recording) -> list[str]:
    """Each message whose integration events differ from what its script states."""
    mismatches: list[str] = []
    for stream in recording.streams:
        for accepted in stream:
            if accepted.event.type != AgentEventType.MESSAGE_ACCEPTED:
                continue
            expected = [
                e.model_dump(mode="json")
                for e in integration_events(Script.model_validate(accepted.event.payload))
            ]
            actual = [
                e.event.model_dump(mode="json")
                for e in stream
                if e.message_id == accepted.message_id and e.event.type in INTEGRATION_EVENT_TYPES
            ]
            if actual != expected:
                mismatches.append(
                    f"message {accepted.message_id} of agent {accepted.agent_id}:\n"
                    f"  script states {expected}\n  stream has    {actual}"
                )
    return mismatches
