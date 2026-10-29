# ABOUTME: Records every conformance scenario against the scripted agent on a local Temporal
# server and checks the recording: the scripted integration published what its scripts state,
# every envelope matches contract/protocol.schema.json, and the normalized streams and send
# results equal the committed goldens. Python's worker hosts the agent unless CONFORMANCE_WORKER
# names another SDK's worker program, as :test_typescript_goldens does for TypeScript's.

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jsonschema
import pytest

from tests.conformance.driver import Recording, normalize, timestamps_never_decrease
from tests.conformance.goldens import (
    GOLDENS,
    UPDATE_COMMAND,
    events_golden,
    external_worker_command,
    integration_mismatches,
    record_all,
    scenario_names,
    sends_golden,
)

NAMES = scenario_names()


@pytest.fixture(scope="module")
def recordings() -> dict[str, Recording]:
    return asyncio.run(record_all(external_worker_command()))


@pytest.fixture(scope="module")
def envelope_validator() -> jsonschema.Validator:
    schema = json.loads(Path("contract/protocol.schema.json").read_text())
    root = {"$defs": schema["$defs"], **schema["event"]}
    return jsonschema.Draft202012Validator(root)


def test_scenarios_exist() -> None:
    assert NAMES


@pytest.mark.parametrize("name", NAMES)
def test_scripted_integration_publishes_what_its_script_states(
    recordings: dict[str, Recording], name: str
) -> None:
    assert integration_mismatches(recordings[name]) == []


@pytest.mark.parametrize("name", NAMES)
def test_every_envelope_matches_the_protocol_schema(
    recordings: dict[str, Recording], envelope_validator: jsonschema.Validator, name: str
) -> None:
    for stream in recordings[name].streams:
        for event in stream:
            envelope_validator.validate(event.model_dump(mode="json"))


@pytest.mark.parametrize("name", NAMES)
def test_timestamps_never_decrease(recordings: dict[str, Recording], name: str) -> None:
    assert timestamps_never_decrease(recordings[name])


@pytest.mark.parametrize("name", NAMES)
def test_recording_matches_its_goldens(recordings: dict[str, Recording], name: str) -> None:
    if integration_mismatches(recordings[name]):
        pytest.skip("the scripted integration diverged from its script")
    normalized = normalize(recordings[name])
    for path, recorded in [
        (events_golden(name), normalized.events_jsonl()),
        (sends_golden(name), normalized.sends_jsonl()),
    ]:
        committed = path.read_text() if path.exists() else ""
        if external_worker_command() is not None:
            assert recorded == committed, f"the recording differs from Python's golden {path}"
        else:
            assert recorded == committed, f"{path} is out of date. To update it, run:\n\n    {UPDATE_COMMAND}\n"


def test_every_golden_has_a_scenario() -> None:
    expected = {events_golden(n).name for n in NAMES} | {sends_golden(n).name for n in NAMES}
    assert {path.name for path in GOLDENS.glob("*.jsonl")} == expected
