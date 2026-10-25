# ABOUTME: Tests of the eval swarm example. A small simulated run (scripted writers, users, judges
# and target, no API keys) goes end to end against a local dev server with the swarm search
# attributes registered: every case is written, run, graded and completed on its category's board
# lane, and one visibility query lists the whole swarm by role. The trace a judge grades from is
# read off a real target's stream, and the report is checked on its own.
#
# Run with: uv run pytest tests/examples/swarm_eval -v

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from temporalio.client import Client
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker, UnsandboxedWorkflowRunner

from temporal_agent_harness.harness.agent_protocol import (
    BOARD_SNAPSHOT_QUERY,
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    BoardItemStatus,
    BoardSnapshot,
)
from temporal_agent_harness.harness.swarm_attributes import AGENT_ROLE, SWARM_SEARCH_ATTRIBUTES
from temporal_agent_harness.plugin import AgentHarnessPlugin

from tests.examples.swarm_eval._incubation_fixtures import experiment, repository
from tests.examples.swarm_eval._incubation_agents import IncidentAgent
from examples.swarm_eval import eval_agents, incubate
from examples.swarm_eval.incubation import EvaluationArtifact, assess, declare_ready
from examples.swarm_eval.promotion import prepare_promotion, snapshot
from examples.swarm_eval.categories import CATEGORIES
from examples.swarm_eval.eval_agents import EVAL_AGENTS, TASK_QUEUE
from examples.swarm_eval.library import EvalLibrary, LibraryActivities, RunRecord, compare
from examples.swarm_eval.models import (
    CaseResult,
    EvalReport,
    EvalRun,
    EvaluationEvidence,
    Verdict,
    Target,
    CaseBatch,
    CaseDraft,
    Grade,
    CriterionResult,
    fingerprint,
)
from examples.swarm_eval.reports import build_report
from examples.swarm_eval.simulated import SCRIPTED_TARGET, ScriptedTravelAgent
from examples.swarm_eval.trace import EvalActivities


@pytest_asyncio.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_local(
        plugins=[AgentHarnessPlugin()], search_attributes=SWARM_SEARCH_ATTRIBUTES
    )
    yield env
    await env.shutdown()


@pytest_asyncio.fixture(autouse=True)
async def worker(env: WorkflowEnvironment) -> AsyncIterator[None]:
    # Per test: a worker runs on the event loop that started it, and each test has its own.
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[*EVAL_AGENTS, ScriptedTravelAgent, EvalLibrary],
        activities=[
            EvalActivities(env.client).collect_tool_calls,
            LibraryActivities(env.client).call_library,
        ],
        max_concurrent_activities=500,
    ):
        yield


async def _reply(client: Client, workflow_id: str, handler: str, payload: dict) -> dict:
    """Send one message and return its reply's output, failing on a handler error."""
    reply: AgentMessageReply = await client.get_workflow_handle(workflow_id).execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type=handler, payload=payload),
        result_type=AgentMessageReply,
    )
    stream = WorkflowStreamClient.create(client, workflow_id)
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
    ):
        envelope: AgentEvent = item.data
        if envelope.message_id != reply.message_id:
            continue
        if envelope.event.type == AgentEventType.MESSAGE_HANDLER_ERROR:
            raise AssertionError(envelope.event.message)
        if envelope.event.type == AgentEventType.MESSAGE_HANDLER_END:
            return envelope.event.output
    raise AssertionError("stream ended before the reply")


async def test_a_simulated_run_writes_runs_and_grades_every_case(env):
    run = EvalRun(cases=16, concurrency=16, judges=2, cases_per_writer=2, simulate=True)
    dispatcher = (
        await env.client.start_workflow(
            "EvalDispatcher", AgentConfig(), id=f"eval-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
    ).id
    report = EvalReport.model_validate(
        await asyncio.wait_for(
            _reply(env.client, dispatcher, "evaluate", run.model_dump(mode="json")), timeout=120
        )
    )

    assert report.swarm_id == dispatcher and report.target == SCRIPTED_TARGET
    assert report.cases == 16
    assert [c.category for c in report.by_category] == [c.key for c in CATEGORIES]
    assert all(c.total == 2 for c in report.by_category)
    assert report.passed == sum(c.passed for c in report.by_category)
    assert report.markdown.startswith("## Simulation complete")
    assert f"**{report.passed} of 16 scenarios passed;" in report.markdown
    assert "do not measure model quality or product fit" in report.markdown
    assert "Simulate off" in report.markdown
    # 8 writers (two cases each), 2 judges, 8 leads, and a tester and a target per case run.
    assert report.agents_started >= 8 + 2 + 8 + 2 * 16

    roles: Counter = Counter()
    for _ in range(50):
        members = [m async for m in env.client.list_workflows(f"SwarmId = '{dispatcher}'")]
        roles = Counter(m.typed_search_attributes.get(AGENT_ROLE) for m in members)
        if roles["target"] >= 16 and roles["board"] == 8:
            break
        await asyncio.sleep(0.2)
    assert roles["dispatcher"] == 1 and roles["writer"] == 8 and roles["judge"] == 2
    assert roles["lead"] == 8 and roles["board"] == 8
    assert roles["tester"] == roles["target"] >= 16

    boards = [m for m in members if m.typed_search_attributes.get(AGENT_ROLE) == "board"]
    for member in boards:
        lane = await env.client.get_workflow_handle(member.id).query(BOARD_SNAPSHOT_QUERY, result_type=BoardSnapshot)
        assert lane.counts[BoardItemStatus.DONE] == 2
        assert all({"passed", "score", "failure_mode"} <= set(i.result or {}) for i in lane.items)
    evidence = EvaluationEvidence.model_validate(await _reply(env.client, dispatcher, "export", {}))
    assert evidence.request.simulate and len(evidence.cases) == len(evidence.results) == 16
    assert evidence.report == report
    assert all(r.tool_calls for r in evidence.results if not r.error)

    # The cases were saved under the run's id; running them again compares the two runs.
    assert report.run_id == f"{dispatcher}#1" and report.suite == report.run_id
    assert report.comparison is None
    again = EvalReport.model_validate(
        await asyncio.wait_for(
            _reply(
                env.client,
                dispatcher,
                "evaluate",
                EvalRun(suite=report.suite, concurrency=16, judges=2, simulate=True).model_dump(
                    mode="json"
                ),
            ),
            timeout=120,
        )
    )
    assert again.run_id == f"{dispatcher}#2" and again.suite == report.suite
    assert again.cases == 16 and again.comparison is not None
    assert again.comparison.previous_run_id == report.run_id
    assert again.comparison.pass_rate_delta == round(again.pass_rate - report.pass_rate, 4)
    assert "**Against" in again.markdown

    # The readers: a run with its evidence, one case in full, and the library's catalog.
    page = (await _reply(env.client, dispatcher, "report", {}))["text"]
    assert page.startswith(again.markdown)
    if again.passed < again.cases:
        assert "## Failures" in page and "**Tools:**" in page
    case_page = (await _reply(env.client, dispatcher, "case", {"case_id": "budget-001"}))["text"]
    assert case_page.startswith("## `budget-001`")
    assert "### Conversation" in case_page and "`search_flights`" in case_page
    catalog = (await _reply(env.client, dispatcher, "suites", {}))["text"]
    assert f"`{report.suite}`: 16 cases" in catalog
    assert f"`{again.run_id}`" in catalog and f"`{report.run_id}`" in catalog


async def test_the_trace_is_every_tool_call_the_target_made(env):
    target = (
        await env.client.start_workflow(
            SCRIPTED_TARGET, AgentConfig(), id=f"target-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
    ).id
    await _reply(env.client, target, "ask", {"text": "SFO to SEA please"})
    await _reply(env.client, target, "ask", {"text": "and again"})

    calls = await EvalActivities(env.client).collect_tool_calls(target)
    assert [c.name for c in calls] == ["search_flights", "search_flights"]
    assert "SFO" in calls[0].input and calls[0].output.startswith("FL-SFOSEA-001")
    assert calls[0].error is None


def test_the_report_ranks_failures_and_counts_modes():
    def result(case_id: str, category: str, passed: bool, score: int, mode: str) -> CaseResult:
        return CaseResult(
            case_id=case_id,
            category=category,
            verdict=Verdict(
                passed=passed, score=score, criteria=[], failure_mode=mode, summary=f"{case_id} summary"
            ),
            turns=2,
            target_workflow_id=f"wf-{case_id}",
            seconds=10,
        )

    report = build_report(
        run_id="d#1",
        suite="d#1",
        swarm_id="s",
        target="MontyChatAgent",
        results=[
            result("budget-001", "budget", False, 3, "ignored_constraint"),
            result("budget-002", "budget", True, 5, "none"),
            result("flight_search-001", "flight_search", False, 1, "hallucinated_result"),
            result("flight_search-002", "flight_search", False, 2, "hallucinated_result"),
        ],
        agents_started=12,
        seconds=61.2,
    )
    assert (report.passed, report.cases, report.pass_rate, report.mean_score) == (1, 4, 0.25, 2.75)
    assert [(c.category, c.passed, c.total) for c in report.by_category] == [
        ("flight_search", 0, 2),
        ("budget", 1, 2),
    ]
    assert report.failure_modes == {"hallucinated_result": 2, "ignored_constraint": 1}
    assert [f.case_id for f in report.failures] == [
        "flight_search-001",
        "flight_search-002",
        "budget-001",
    ]
    assert "| Budget limit | 1/2 |" in report.markdown
    assert report.markdown.startswith("## Evaluation results")
    assert "Passing these tests alone does not establish product fit" in report.markdown
    assert "**1 of 4 scenarios passed; 3 failed.**" in report.markdown
    assert "generated by the simulator" not in report.markdown


def test_a_comparison_names_what_was_fixed_and_what_regressed():
    def record(run_id: str, passed: dict[str, bool]) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            suite="s",
            target="T",
            finished_at=0,
            models={},
            pass_rate=sum(passed.values()) / len(passed),
            mean_score=3,
            outcomes={
                c: {"passed": p, "score": 5 if p else 2, "failure_mode": "none" if p else "no_action"}
                for c, p in passed.items()
            },
        )

    before = record("d#1", {"a": True, "b": False, "c": False, "gone": True})
    after = record("d#2", {"a": False, "b": True, "c": False, "new": True})
    comparison = compare(before, after)
    assert (comparison.fixed, comparison.regressed, comparison.still_failing) == (["b"], ["a"], ["c"])
    assert comparison.previous_run_id == "d#1"
    assert comparison.pass_rate_delta == 0.0


async def test_custom_target_completes_incubation_and_prepares_promotion(env, repository, monkeypatch):
    """Real Temporal orchestration, deterministic model doubles, and explicit fixture fit evidence."""
    queue = f"incubation-test-{uuid.uuid4()}"
    artifact, fit = experiment(snapshot(repository))
    target = Target(workflow_type="IncubationTestTarget", task_queue=queue, handler="investigate",
                    input_field="question", output_field="answer", model_handler=None,
                    init_data={"environment": "staging"})
    artifact.candidate.evaluation = artifact.candidate.evaluation.model_copy(update={"target": target, "max_turns": 1})
    candidate = artifact.candidate

    async def model_double(runner, *, name, instructions, model, output_type, prompt):
        if output_type is CaseBatch:
            return CaseBatch(cases=[CaseDraft(persona="operator", goal="Diagnose a failure", opening="Why did charging fail?", success_criteria=["Cites charge timeout"])])
        grade = Grade.model_validate_json(prompt)
        assert grade.tool_calls[0].output == "staging: charge timed out"
        assert "staging: charge timed out" in grade.conversation[0].agent
        return Verdict(passed=True, score=5, failure_mode="none", summary="Cites evidence",
                       criteria=[CriterionResult(criterion=grade.case.success_criteria[0], met=True, evidence=grade.tool_calls[0].output)])

    async def connect(**kwargs):
        return env.client

    monkeypatch.setattr(eval_agents, "TASK_QUEUE", queue)
    monkeypatch.setattr(eval_agents, "_structured", model_double)
    monkeypatch.setattr(incubate.Client, "connect", connect)
    async with Worker(env.client, task_queue=queue, workflows=[*EVAL_AGENTS, EvalLibrary, IncidentAgent],
                      activities=[EvalActivities(env.client).collect_tool_calls, LibraryActivities(env.client).call_library],
                      workflow_runner=UnsandboxedWorkflowRunner()):
        output = repository / ".incubation/evaluation.json"
        await incubate.evaluate(repository, candidate, output, task_queue=queue, timeout=30)
        exported = EvaluationArtifact.model_validate_json(output.read_text())
        assert exported.evidence.target == target
        assert exported.evidence.report.passed == 1
        fit.evaluation_hash = fingerprint(exported)
        assert assess(exported, fit).eligible
        declaration = declare_ready(exported, fit, by="TEST FIXTURE", rationale="Test the explicit readiness transition")
        patch = prepare_promotion(repository, exported, fit, declaration)
        assert "products/incident-assistant/examples/incident/agent.py" in patch


async def test_a_failed_experiment_does_not_reuse_its_saved_suite_id(env):
    dispatcher = (await env.client.start_workflow(
        "EvalDispatcher", AgentConfig(), id=f"retry-eval-{uuid.uuid4()}", task_queue=TASK_QUEUE
    )).id
    too_large = EvalRun(cases=2, max_cases=1, categories=["flight_search"], concurrency=1,
                        judges=1, max_turns=1, simulate=True)
    with pytest.raises(AssertionError, match="suite exceeds"):
        await asyncio.wait_for(_reply(env.client, dispatcher, "evaluate", too_large.model_dump()), timeout=30)
    corrected = too_large.model_copy(update={"cases": 1})
    report = EvalReport.model_validate(await asyncio.wait_for(
        _reply(env.client, dispatcher, "evaluate", corrected.model_dump()), timeout=30
    ))
    assert report.run_id == report.suite == f"{dispatcher}#2"
    assert report.cases == 1
