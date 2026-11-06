# ABOUTME: test_goldens against the LangGraph scripted worker: the same scenarios, checks and
# ABOUTME: goldens, so the LangGraph glue's stream matches Python's event for event.
#
# Under plain pytest the worker is this module's own; Bazel names it in CONFORMANCE_WORKER.

from __future__ import annotations

import asyncio
import os
import shlex
import sys

import pytest

from tests.conformance.driver import Recording
from tests.conformance.goldens import WORKER_ENV, external_worker_command, record_all
from tests.conformance.test_goldens import (  # noqa: F401 — collected here too
    envelope_validator,
    test_every_envelope_matches_the_protocol_schema,
    test_every_golden_has_a_scenario,
    test_recording_matches_its_goldens,
    test_scenarios_exist,
    test_scripted_integration_publishes_what_its_script_states,
    test_timestamps_never_decrease,
)

@pytest.fixture(scope="module")
def recordings() -> dict[str, Recording]:
    command = external_worker_command()
    if command is None:
        command = [sys.executable, "-m", "tests.conformance.langgraph_worker"]
        os.environ[WORKER_ENV] = shlex.join(command)
    assert "langgraph_worker" in " ".join(command), f"{WORKER_ENV} must name langgraph_worker"
    return asyncio.run(record_all(command))
