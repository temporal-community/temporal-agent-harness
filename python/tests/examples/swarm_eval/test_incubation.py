"""Readiness requires real evidence; promotion binds that evidence to the reviewed checkout."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import uuid
import subprocess

import pytest
from temporalio.exceptions import ApplicationError

from tests.examples.swarm_eval._incubation_fixtures import experiment, repository

from examples.swarm_eval import eval_agents
from examples.swarm_eval.trace import EvalActivities
from temporal_agent_harness.harness.agent_protocol import AgentEventType, TURN_EVENTS_TOPIC
from examples.swarm_eval.incubation import (
    assess,
    declare_ready,
    validate_declaration,
)
from examples.swarm_eval.incubate import write_artifact
from examples.swarm_eval.library import EvalLibrary, LibraryState, Suite
from examples.swarm_eval.models import (
    EvalRun,
    Grade,
    LaneReport,
    fingerprint,
    validate_verdict,
)
from examples.swarm_eval.promotion import prepare_promotion, snapshot
from examples.swarm_eval.reports import run_record, render_case


def test_readiness_is_explicit_and_pins_every_input():
    artifact, fit = experiment()
    assert assess(artifact, fit).eligible
    declaration = declare_ready(artifact, fit, by="Test operator", rationale="Fixture gate exercise")
    validate_declaration(artifact, fit, declaration)
    artifact.candidate.owner = "Different owner"
    fit.evaluation_hash = fingerprint(artifact)
    with pytest.raises(ValueError, match="does not match"):
        validate_declaration(artifact, fit, declaration)


@pytest.mark.parametrize("problem", ["simulation", "synthetic", "missing_fit", "wrong_run", "source", "cost", "latency", "criteria", "execution", "coverage", "duplicate", "target", "config", "missing_results"])
def test_readiness_fails_closed(problem):
    artifact, fit = experiment()
    if problem == "simulation":
        artifact.evidence.request.simulate = True
    elif problem == "synthetic":
        fit.kind = "synthetic"
    elif problem == "missing_fit":
        fit.findings.pop()
    elif problem == "wrong_run":
        fit.candidate_slug = "different-candidate"
    elif problem == "source":
        artifact.source.commit = "changed"
    elif problem == "cost":
        fit.measured_cost_usd = 100
    elif problem == "latency":
        artifact.evidence.report.seconds = 10000
    elif problem == "criteria":
        artifact.evidence.results[0].verdict.criteria.clear()
    elif problem == "execution":
        artifact.evidence.results[0].error = "target crashed"
    elif problem == "coverage":
        artifact.candidate.required_categories.append("recovery")
    elif problem == "duplicate":
        artifact.evidence.results.append(artifact.evidence.results[0])
    elif problem == "target":
        artifact.evidence.target = artifact.evidence.target.model_copy(update={"workflow_type": "OtherAgent"})
    elif problem == "config":
        artifact.evidence.request = artifact.evidence.request.model_copy(update={"max_turns": 8})
    elif problem == "missing_results":
        artifact.evidence.results.clear()
    fit.evaluation_hash = fingerprint(artifact)
    assert not assess(artifact, fit).eligible
    with pytest.raises(ValueError, match="not ready"):
        declare_ready(artifact, fit, by="operator", rationale="Must fail")


def test_judge_cannot_override_missing_criteria_or_execution_error():
    artifact, _ = experiment()
    evidence = artifact.evidence
    verdict = evidence.results[0].verdict
    grade = Grade(case=evidence.cases[0], rubric="diagnose", conversation=[], tool_calls=[])
    assert validate_verdict(verdict, grade).passed
    assert not validate_verdict(verdict.model_copy(update={"criteria": []}), grade).passed
    assert not validate_verdict(verdict, grade.model_copy(update={"error": "failure"})).passed
    assert "**Category:** diagnosis" in render_case(evidence.cases[0], evidence.results[0])


def test_suites_are_immutable_and_run_retries_preserve_the_original_comparison():
    artifact, _ = experiment()
    library = EvalLibrary(LibraryState(target="IncidentAgent"))
    suite = Suite(name="release", target="IncidentAgent", created_at=0, cases=artifact.evidence.cases)
    library.save_suite(suite)
    library.save_suite(suite.model_copy(update={"created_at": 1}))
    changed = suite.model_copy(deep=True)
    changed.cases[0].goal = "Different test"
    with pytest.raises(ApplicationError, match="different cases"):
        library.save_suite(changed)
    record = run_record(artifact.evidence.report, artifact.evidence.results, {}, 0)
    record.suite_hash = artifact.evidence.suite_hash
    assert library.record_run(record) is None
    second = record.model_copy(update={"run_id": "test#2"})
    comparison = library.record_run(second)
    assert comparison.previous_run_id == "test#1"
    assert library.record_run(second) == comparison
    third = record.model_copy(update={"run_id": "test#3", "evaluator_hash": "new-judge"})
    assert library.record_run(third) is None


def test_artifacts_do_not_overwrite_existing_evidence(tmp_path):
    path = tmp_path / "evidence.json"
    artifact, _ = experiment()
    write_artifact(path, artifact)
    write_artifact(path, artifact)
    artifact.candidate.name = "Changed"
    with pytest.raises(ValueError, match="refusing to replace"):
        write_artifact(path, artifact)


def test_promotion_patch_applies_and_relocates_only_after_a_valid_declaration(repository):
    artifact, fit = experiment(snapshot(repository))
    declaration = declare_ready(artifact, fit, by="operator", rationale="Fixture evidence passes")
    patch = prepare_promotion(repository, artifact, fit, declaration)
    patch_path = repository / ".incubation/promotion.patch"
    write_artifact(patch_path, patch)
    subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=repository, check=True)
    assert (repository / "python/examples/incident/agent.py").exists()
    subprocess.run(["git", "apply", str(patch_path)], cwd=repository, check=True)
    assert not (repository / "python/examples/incident/agent.py").exists()
    assert (repository / "products/incident-assistant/examples/incident/agent.py").exists()
    assert (repository / "products/incident-assistant/examples/incident/empty.py").exists()
    assert (repository / "products/incident-assistant/examples/incident/no_newline.txt").read_text() == "no newline"
    assert '"incident"' not in (repository / "python/examples/BUILD.bazel").read_text()
    assert (repository / "products/incident-assistant/readiness.json").exists()


def test_promotion_rejects_a_changed_checkout(repository):
    artifact, fit = experiment(snapshot(repository))
    declaration = declare_ready(artifact, fit, by="operator", rationale="Fixture evidence passes")
    (repository / "python/examples/incident/agent.py").write_text("# changed\n")
    with pytest.raises(ValueError, match="checkout changed"):
        prepare_promotion(repository, artifact, fit, declaration)


@pytest.mark.parametrize("concurrency", [1, 2])
async def test_scheduler_enforces_a_global_conversation_limit(monkeypatch, concurrency):
    artifact, _ = experiment()
    profile = artifact.evidence.profile
    cases = [artifact.evidence.cases[0].model_copy(update={"id": str(i), "category": f"lane_{i}"}) for i in range(4)]
    active = 0
    peak = 0
    specs = {}
    stopped = []

    class Runner:
        current_status = SimpleNamespace(subagents=[])

        async def start_subagent(self, key, workflow_type, task_queue, *, data):
            handle = str(len(specs))
            specs[handle] = data
            self.current_status.subagents.append(SimpleNamespace(subagent_id=handle, workflow_id=handle))
            return handle

        async def stop_subagent(self, handle):
            stopped.append(handle)

        async def run_subagent_turn(self, handle, handler, payload):
            nonlocal active, peak
            active += specs[handle].slots
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= specs[handle].slots
            return LaneReport(category=specs[handle].category, results=[], testers_started=0).model_dump()

    class Board:
        def __init__(self, *args, **kwargs):
            pass

        async def post_many(self, posts):
            return []

    dispatcher = object.__new__(eval_agents.EvalDispatcher)
    dispatcher._runner = Runner()
    monkeypatch.setattr(eval_agents.agent, "SwarmBoard", Board)
    monkeypatch.setattr(eval_agents.workflow, "uuid4", uuid.uuid4)
    await dispatcher._run_cases(EvalRun(concurrency=concurrency, judges=1), artifact.evidence.target, "s", cases, profile)
    assert peak == concurrency
    assert len(stopped) == 5


async def test_trace_preserves_late_calls_and_full_outputs(monkeypatch):
    output = "x" * 2000 + "CONFIRMATION-AT-THE-END"
    events = []
    for i in range(35):
        events += [SimpleNamespace(type=AgentEventType.TOOL_START, tool_id=str(i), tool_name="book", tool_input=output),
                   SimpleNamespace(type=AgentEventType.TOOL_END, tool_id=str(i), tool_output=output)]

    class Stream:
        async def get_offset(self):
            return len(events) + 1

        async def subscribe(self, **kwargs):
            for offset, event in enumerate(events):
                yield SimpleNamespace(offset=offset, topic=TURN_EVENTS_TOPIC, data=SimpleNamespace(event=event))
            yield SimpleNamespace(offset=len(events), topic="other-topic", data={})
            raise AssertionError("collector read past the captured global offset")

    monkeypatch.setattr("examples.swarm_eval.trace.AgentEvent.model_validate", lambda value: value)
    monkeypatch.setattr("examples.swarm_eval.trace.WorkflowStreamClient.create", lambda *args: Stream())
    calls = await EvalActivities(None).collect_tool_calls("target")
    assert len(calls) == 35
    assert calls[-1].output == calls[-1].input == output
