"""The messages, init data and results of the eval swarm's agents."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------- the run


class Models(BaseModel):
    """Model choices for each role. Defaults use Gemini for Monty and OpenAI for evaluators;
    custom targets use the provider configured on their own worker."""

    target: str = Field(
        default="gemini-3.1-flash-lite",
        description="The target agent's model, set on each instance before its first turn.",
    )
    writer: str = Field(default="gpt-6-luna", description="Invents the test cases.")
    tester: str = Field(default="gpt-6-luna", description="Plays the user in each case.")
    judge: str = Field(default="gpt-6.1-sol", description="Grades each case against its rubric.")


class Target(BaseModel):
    """The agent under test, and how to drive it."""

    workflow_type: str
    task_queue: str
    handler: str = "ask"
    input_field: str = "text"
    output_field: str = "text"
    init_data: dict[str, Any] | None = None
    model_handler: str | None = "set_model"


class Category(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    brief: str
    rubric: str


class EvaluationProfile(BaseModel):
    description: str = Field(min_length=1)
    categories: list[Category] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_categories(self) -> EvaluationProfile:
        if len({c.key for c in self.categories}) != len(self.categories):
            raise ValueError("category keys must be unique")
        return self


def fingerprint(value: Any) -> str:
    """Stable identity for JSON evidence and evaluation contracts."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class EvalRun(BaseModel):
    """Evaluate a configured agent with a swarm: write cases, run each as a live conversation
    with a fresh agent, and grade every one."""

    target: Target | None = None
    profile: EvaluationProfile | None = None
    revision: str = Field(default="", description="Evaluated source revision; supplied by the incubation CLI.")
    max_cases: int | None = Field(default=None, ge=1, description="Optional cap, including when loading a saved suite.")
    cases: int = Field(default=16, ge=1, le=1000, description="Test cases across all categories.")
    categories: list[str] = Field(
        default_factory=list,
        description="Category keys to test (see categories.py); empty tests every category.",
    )
    concurrency: int = Field(
        default=2,
        ge=1,
        le=1000,
        description="Conversations in flight at once. At most this many target agents run, "
        "which is what bounds the request rate to the models.",
    )
    max_turns: int = Field(default=3, ge=1, le=8, description="User messages per conversation.")
    judges: int = Field(default=1, ge=1, le=64, description="Judges the testers share.")
    cases_per_writer: int = Field(default=10, ge=1, le=40)
    models: Models = Field(default_factory=Models)
    suite: str | None = Field(
        default=None,
        description="Run this saved suite's cases instead of writing new ones (`cases` and "
        "`categories` are then ignored). Every run saves its cases as a suite; see `save_as`.",
    )
    save_as: str | None = Field(
        default=None,
        description="Save the cases this run writes as a suite with this name. Without it they "
        "are saved under the run's id. Ignored when `suite` is given.",
    )
    simulate: bool = Field(
        default=False,
        description="Script the writers, testers and judges instead of calling a model, and "
        "test the scripted target: a run with no API keys, for trying the swarm itself.",
    )


# ---------------------------------------------------------------- cases


class CaseDraft(BaseModel):
    """One test conversation, as a writer invents it."""

    persona: str = Field(description="Who the user is, in a sentence.")
    goal: str = Field(
        description="What the user wants, and every detail they know (names, airports, dates, "
        "preferences), including any they hold back until asked."
    )
    opening: str = Field(description="The user's first message, in their voice.")
    success_criteria: list[str] = Field(
        description="Two to four concrete checks a judge can verify from the agent's tool calls "
        "and replies."
    )


class CaseBatch(BaseModel):
    """A writer's cases for one category."""

    cases: list[CaseDraft]


class EvalCase(CaseDraft):
    """A case as the swarm runs it: a draft with its id, category and turn budget."""

    id: str
    category: str
    max_turns: int


class WriteCases(BaseModel):
    """Write ``count`` cases for one category; ``batch`` keeps parallel writers from repeating."""

    category: str
    definition: Category | None = None
    count: int
    batch: int


# ---------------------------------------------------------------- conversations


class Exchange(BaseModel):
    """One user message and the agent's reply to it."""

    user: str
    agent: str


class ToolCall(BaseModel):
    """One tool call the target made, from its own event stream, preserved in full."""

    name: str
    input: str
    output: str | None = None
    error: str | None = None


class TesterMove(BaseModel):
    """The simulated user's next step."""

    done: bool = Field(
        description="True when the conversation has reached the user's goal, or cannot."
    )
    message: str = Field(default="", description="The user's next message, when not done.")


FailureMode = Literal[
    "none",
    "hallucinated_result",
    "ignored_constraint",
    "wrong_action",
    "no_action",
    "did_not_ask",
    "unsupported_claim",
    "error",
]


class CriterionResult(BaseModel):
    """Whether one success criterion was met, and the evidence."""

    criterion: str
    met: bool
    evidence: str = Field(description="What in the tool calls or replies shows it.")


class Verdict(BaseModel):
    """A judge's grade for one case."""

    passed: bool
    score: int = Field(ge=1, le=5, description="5 is flawless, 1 is wrong or harmful.")
    criteria: list[CriterionResult]
    failure_mode: FailureMode = Field(description="The main way it failed; 'none' if it passed.")
    summary: str = Field(description="One sentence a developer can act on.")


class Grade(BaseModel):
    """Grade one finished case."""

    case: EvalCase
    rubric: str
    conversation: list[Exchange]
    tool_calls: list[ToolCall]
    error: str | None = None


# ---------------------------------------------------------------- init data




class WriterSpec(BaseModel):
    model: str
    simulate: bool
    description: str = ""


class JudgeSpec(BaseModel):
    model: str
    simulate: bool
    description: str = ""


class TesterSpec(BaseModel):
    target: Target
    profile: EvaluationProfile
    target_model: str | None
    model: str
    judges: list[str] = Field(description="Workflow ids of the judges.")
    simulate: bool


class LaneSpec(BaseModel):
    """A lead's init data: its category's lane and how many conversations to keep going."""

    category: str
    lane: str | None = None
    slots: int
    tester: TesterSpec


# ---------------------------------------------------------------- results


class RunLane(BaseModel):
    """Run every case in this lead's lane, then report."""


class CaseResult(BaseModel):
    """One graded case, as its tester reports it, with the evidence it was graded on."""

    case_id: str
    category: str
    verdict: Verdict
    turns: int
    target_workflow_id: str
    seconds: float
    conversation: list[Exchange] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    error: str | None = None


class LaneReport(BaseModel):
    """Every case one lead ran, and how many testers it started."""

    category: str
    results: list[CaseResult]
    testers_started: int


class CategoryResult(BaseModel):
    """Pass count for one category."""

    category: str
    title: str
    passed: int
    total: int


class Failure(BaseModel):
    """One failed case, for the report."""

    case_id: str
    category: str
    failure_mode: FailureMode
    summary: str
    target_workflow_id: str = Field(description="The target agent that failed, to look at.")


class Comparison(BaseModel):
    """A run against the previous run of the same suite."""

    previous_run_id: str
    previous_pass_rate: float
    pass_rate_delta: float
    fixed: list[str] = Field(description="Cases that failed before and pass now.")
    regressed: list[str] = Field(description="Cases that passed before and fail now.")
    still_failing: list[str]


class EvalReport(BaseModel):
    """How the target did."""

    run_id: str = Field(description="'<dispatcher workflow id>#<n>'; `report` and `case` take it.")
    suite: str = Field(description="The suite this run's cases are saved as, to run again.")
    comparison: Comparison | None = Field(
        default=None, description="Against the previous run of the same suite, if there is one."
    )
    swarm_id: str
    target: str
    cases: int
    passed: int
    pass_rate: float
    mean_score: float
    by_category: list[CategoryResult]
    failure_modes: dict[str, int]
    failures: list[Failure] = Field(description="The lowest-scoring failures, worst first.")
    agents_started: int
    seconds: float
    failed_batches: int = Field(default=0, description="Batches of cases a writer failed to write.")
    markdown: str



# ---------------------------------------------------------------- reading results


class ReportRequest(BaseModel):
    """Show a run's report, with each failure's evidence."""

    run_id: str = Field(default="", description="A run of this session; empty for the latest.")


class CaseRequest(BaseModel):
    """Show everything about one case: its goal, verdict, conversation and tool calls."""

    case_id: str
    run_id: str = Field(default="", description="A run of this session; empty for the latest.")


class ListSuites(BaseModel):
    """List the saved suites and the recent runs, across every session."""

    runs: int = Field(default=10, ge=1, le=100)


class EvaluationEvidence(BaseModel):
    """Full, exportable evidence. Simulation is explicitly recorded and never certifies readiness."""

    report: EvalReport
    request: EvalRun
    target: Target
    profile: EvaluationProfile
    cases: list[EvalCase]
    results: list[CaseResult]

    @property
    def suite_hash(self) -> str:
        return fingerprint({"target": self.target.model_dump(mode="json"), "profile": self.profile.model_dump(mode="json"), "cases": [c.model_dump(mode="json") for c in self.cases]})


def validate_verdict(verdict: Verdict, grade: Grade) -> Verdict:
    """Fail closed on model output that omits checks or contradicts the observed outcome."""
    expected = grade.case.success_criteria
    actual = [c.criterion for c in verdict.criteria]
    consistent = (
        bool(expected) and sorted(actual) == sorted(expected)
        and all(c.met for c in verdict.criteria)
        and grade.error is None and verdict.score >= 4 and verdict.failure_mode == "none"
    )
    if verdict.passed and not consistent:
        return verdict.model_copy(update={"passed": False, "score": min(verdict.score, 3), "failure_mode": "error", "summary": "Invalid passing verdict: missing/unmet criteria, execution error, or inconsistent score."})
    if grade.error:
        return verdict.model_copy(update={"passed": False, "failure_mode": "error", "score": 1})
    return verdict
