"""Synthetic readiness fixtures used only by tests; no real product-fit claims."""
from __future__ import annotations

import os
import subprocess

import pytest

from examples.swarm_eval.incubation import (
    Candidate,
    EvaluationArtifact,
    FIT_DIMENSIONS,
    FitAssessment,
    FitFinding,
    SourceSnapshot,
)
from examples.swarm_eval.models import (
    Category,
    CaseResult,
    CriterionResult,
    EvalCase,
    EvaluationEvidence,
    EvaluationProfile,
    EvalRun,
    Target,
    Verdict,
    fingerprint,
)
from examples.swarm_eval.reports import build_report

def experiment(source: SourceSnapshot | None = None) -> tuple[EvaluationArtifact, FitAssessment]:
    """Positive gate fixture, not a claim of actual user research or a live model evaluation."""
    profile = EvaluationProfile(description="Read-only Temporal incident diagnosis", categories=[
        Category(key="diagnosis", title="Diagnosis", brief="Inspect a failure", rubric="Cite the failed activity"),
    ])
    target = Target(workflow_type="IncidentAgent", task_queue="incidents", handler="investigate", model_handler=None)
    run = EvalRun(cases=1, concurrency=1, judges=1, target=target, profile=profile)
    candidate = Candidate(
        slug="incident-assistant", name="Incident assistant", owner="Test owner", example="incident",
        target_user="Temporal operators", problem="Slow diagnosis", workaround="Read history manually",
        temporal_advantage="Durable investigation", harness_advantage="Typed tools",
        adoption_hypothesis="Operators can diagnose a failed activity", evaluation=run,
        min_cases=1, required_categories=["diagnosis"],
    )
    source = source or SourceSnapshot(commit="test-commit", files={"python/examples/incident/agent.py": "test-sha"})
    case = EvalCase(id="diagnosis-001", category="diagnosis", max_turns=3, persona="operator",
                    goal="Find the failed activity", opening="Why did it fail?", success_criteria=["Cites the failed activity"])
    verdict = Verdict(passed=True, score=5, criteria=[CriterionResult(criterion=case.success_criteria[0], met=True, evidence="tool: activity=charge")], failure_mode="none", summary="Correctly diagnosed")
    result = CaseResult(case_id=case.id, category=case.category, verdict=verdict, turns=1, target_workflow_id="test-target", seconds=1)
    report = build_report(run_id="test#1", suite="diagnosis-v1", swarm_id="test", target=target.workflow_type,
                          results=[result], agents_started=5, seconds=1, categories=profile.categories)
    evidence = EvaluationEvidence(report=report, request=run.model_copy(update={"revision": source.digest}),
                                  target=target, profile=profile, cases=[case], results=[result])
    artifact = EvaluationArtifact(candidate=candidate, source=source, evidence=evidence)
    fit = FitAssessment(candidate_slug=candidate.slug, evaluation_hash=fingerprint(artifact), kind="observed",
                        assessed_by="Test assessor", measured_cost_usd=0.01, cost_reference="fixture://usage",
                        findings=[FitFinding(dimension=d, outcome="supported", observation="Test fixture observation", references=["fixture://research"]) for d in FIT_DIMENSIONS])
    return artifact, fit


@pytest.fixture
def repository(tmp_path, monkeypatch):
    # Git is a local CLI dependency of the promotion tool, never used for network operations.
    monkeypatch.setenv("PATH", os.defpath)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    files = {
        ".gitignore": ".incubation/\n",
        "python/examples/BUILD.bazel": 'EXAMPLES = [\n    "incident",\n]\n',
        "python/examples/incident/BUILD.bazel": 'load("@aspect_rules_py//py:defs.bzl", "py_library")\npy_library(name="agent", srcs=["agent.py"], imports=["../.."], visibility=["//python:__subpackages__"])\n',
        "python/examples/incident/agent.py": '"""Test fixture agent."""\n',
        "python/examples/incident/README.md": "Run //python/examples/incident:worker\n",
        "python/examples/incident/empty.py": "",
        "python/examples/incident/no_newline.txt": "no newline",
        "README.md": "See python/examples/incident/README.md",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    return tmp_path

