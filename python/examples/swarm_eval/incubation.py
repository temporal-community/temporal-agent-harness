"""Product hypotheses and explicit readiness decisions, independent of the Temporal runtime.

The local operator owns declarations. Models can supply evidence, but cannot declare a product
ready through an agent tool. Every decision binds the candidate, source, evaluation and fit data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from examples.swarm_eval.models import EvaluationEvidence, EvalRun, fingerprint


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    name: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    example: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    target_user: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    workaround: str = Field(min_length=1)
    temporal_advantage: str = Field(min_length=1)
    harness_advantage: str = Field(min_length=1)
    adoption_hypothesis: str = Field(min_length=1)
    evaluation: EvalRun
    min_cases: int = Field(default=8, ge=1)
    min_pass_rate: float = Field(default=0.9, ge=0, le=1)
    required_categories: list[str] = Field(min_length=1)
    max_run_seconds: float = Field(default=300, gt=0)
    max_cost_usd: float = Field(default=5, ge=0)

    @model_validator(mode="after")
    def small_run(self) -> Candidate:
        if self.evaluation.cases > 32 or self.evaluation.concurrency > 4 or self.evaluation.judges > 2:
            raise ValueError("the incubation milestone supports at most 32 cases, 4 conversations, and 2 judges")
        if self.evaluation.suite and (self.evaluation.max_cases is None or self.evaluation.max_cases > 32):
            raise ValueError("saved-suite experiments require max_cases <= 32")
        if len(set(self.required_categories)) != len(self.required_categories):
            raise ValueError("required categories must be unique")
        return self


class SourceSnapshot(BaseModel):
    commit: str
    files: dict[str, str]

    @property
    def digest(self) -> str:
        return fingerprint(self)


class EvaluationArtifact(BaseModel):
    candidate: Candidate
    source: SourceSnapshot
    evidence: EvaluationEvidence


FitDimension = Literal["problem", "temporal_advantage", "harness_advantage", "adoption", "operability"]
FIT_DIMENSIONS: tuple[FitDimension, ...] = (
    "problem", "temporal_advantage", "harness_advantage", "adoption", "operability"
)


class FitFinding(BaseModel):
    dimension: FitDimension
    outcome: Literal["supported", "unsupported", "unknown"]
    observation: str = Field(min_length=1)
    references: list[str] = Field(min_length=1, description="Links or artifact paths backing the observation.")


class FitAssessment(BaseModel):
    """An operator's evidence assessment. Synthetic interviews never satisfy a readiness gate."""

    candidate_slug: str
    evaluation_hash: str
    kind: Literal["observed", "synthetic"]
    assessed_by: str = Field(min_length=1)
    findings: list[FitFinding]
    measured_cost_usd: float = Field(ge=0)
    cost_reference: str = Field(min_length=1)


class Readiness(BaseModel):
    eligible: bool
    blockers: list[str]


def assess(artifact: EvaluationArtifact, fit: FitAssessment) -> Readiness:
    candidate, evidence = artifact.candidate, artifact.evidence
    blockers: list[str] = []
    if fit.candidate_slug != candidate.slug or fit.evaluation_hash != fingerprint(artifact):
        blockers.append("product-fit evidence belongs to a different candidate/evaluation")
    if evidence.request.simulate or fit.kind != "observed":
        blockers.append("simulation/synthetic evidence cannot establish product readiness")
    if evidence.request.revision != artifact.source.digest:
        blockers.append("evaluation is not pinned to this source snapshot")
    if evidence.request.model_dump(exclude={"revision"}) != candidate.evaluation.model_dump(exclude={"revision"}):
        blockers.append("evaluation configuration differs from the candidate's declared experiment")
    if candidate.evaluation.target is not None and evidence.target != candidate.evaluation.target:
        blockers.append("evaluation target differs from the declared target")
    if candidate.evaluation.profile is not None and evidence.profile != candidate.evaluation.profile:
        blockers.append("evaluation profile differs from the declared profile")
    if evidence.report.target != evidence.target.workflow_type:
        blockers.append("report target differs from the evaluation target")
    if candidate.evaluation.target is None and evidence.target.workflow_type != (
        "ScriptedTravelAgent" if candidate.evaluation.simulate else "MontyChatAgent"
    ):
        blockers.append("evaluation does not use the default target declared by the candidate")
    known_categories = {c.key for c in evidence.profile.categories}
    if any(c.category not in known_categories or c.max_turns != evidence.request.max_turns for c in evidence.cases):
        blockers.append("cases do not match the evaluation profile or turn budget")
    cases = {c.id: c for c in evidence.cases}
    results = {r.case_id: r for r in evidence.results}
    if (len(cases) != len(evidence.cases) or len(results) != len(evidence.results)
            or set(cases) != set(results)):
        blockers.append("case results are missing, duplicated, or do not match the suite")
    if len(cases) < max(candidate.min_cases, evidence.request.cases) and not evidence.request.suite:
        blockers.append("the generated suite did not meet the requested coverage")
    if len(cases) < candidate.min_cases:
        blockers.append("too few evaluation cases")
    if evidence.report.failed_batches:
        blockers.append("some case-writing batches failed")
    passed = 0
    for case_id, result in results.items():
        case = cases.get(case_id)
        if case is None:
            continue
        if result.category != case.category:
            blockers.append(f"{case_id}: result category differs from its case")
        verdict = result.verdict
        if result.error or verdict.failure_mode == "error":
            blockers.append(f"{case_id}: execution or grading error")
        if verdict.passed:
            if (not case.success_criteria or sorted(c.criterion for c in verdict.criteria) != sorted(case.success_criteria)
                    or not all(c.met for c in verdict.criteria) or verdict.score < 4 or verdict.failure_mode != "none"):
                blockers.append(f"{case_id}: inconsistent passing verdict")
            else:
                passed += 1
    rate = passed / len(cases) if cases else 0
    if rate < candidate.min_pass_rate:
        blockers.append("functional pass rate is below the declared threshold")
    if (evidence.report.cases != len(cases) or evidence.report.passed != passed
            or abs(evidence.report.pass_rate - rate) > 0.0001):
        blockers.append("report totals do not match the case evidence")
    for category in candidate.required_categories:
        relevant = [r for r in results.values() if r.category == category]
        if not relevant or sum(r.verdict.passed for r in relevant) / len(relevant) < candidate.min_pass_rate:
            blockers.append(f"required category {category!r} lacks passing coverage")
    if evidence.report.seconds > candidate.max_run_seconds:
        blockers.append("run duration exceeds the declared limit")
    if fit.measured_cost_usd > candidate.max_cost_usd:
        blockers.append("measured cost exceeds the declared limit")
    dimensions = [finding.dimension for finding in fit.findings]
    if len(dimensions) != len(set(dimensions)):
        blockers.append("duplicate product-fit dimensions")
    for dimension in FIT_DIMENSIONS:
        finding = next((f for f in fit.findings if f.dimension == dimension), None)
        if finding is None or finding.outcome != "supported":
            blockers.append(f"product fit is unproven: {dimension}")
    return Readiness(eligible=not blockers, blockers=blockers)


class ReadyDeclaration(BaseModel):
    candidate_slug: str
    evaluation_hash: str
    fit_hash: str
    source_hash: str
    declared_by: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    declared_at: str


def declare_ready(artifact: EvaluationArtifact, fit: FitAssessment, *, by: str, rationale: str) -> ReadyDeclaration:
    readiness = assess(artifact, fit)
    if not readiness.eligible:
        raise ValueError("not ready: " + "; ".join(readiness.blockers))
    return ReadyDeclaration(
        candidate_slug=artifact.candidate.slug, evaluation_hash=fingerprint(artifact),
        fit_hash=fingerprint(fit), source_hash=artifact.source.digest,
        declared_by=by, rationale=rationale, declared_at=datetime.now(timezone.utc).isoformat(),
    )


def validate_declaration(artifact: EvaluationArtifact, fit: FitAssessment, declaration: ReadyDeclaration) -> None:
    readiness = assess(artifact, fit)
    if not readiness.eligible:
        raise ValueError("not ready: " + "; ".join(readiness.blockers))
    if (declaration.candidate_slug != artifact.candidate.slug
            or declaration.evaluation_hash != fingerprint(artifact)
            or declaration.fit_hash != fingerprint(fit)
            or declaration.source_hash != artifact.source.digest):
        raise ValueError("declaration does not match the candidate, source and evidence")
