"""The eval library: saved case suites and the record of every run, per agent under test.

Cases are written fresh by models each run, so two runs' pass rates differ partly because their
cases did. To see whether a CHANGE to the agent helped, run it against the same cases again: every
run saves the cases it used as a suite (named after the run, or `EvalRun.save_as`), and a later
run can name that suite instead of writing new ones.

Every run is also recorded, case by case, and recording returns how it compares with the previous
run of the same suite — which cases were fixed, which regressed — so the report can say what the
change did. The library keeps outcomes, not evidence: a case's conversation and tool calls stay
with the dispatcher session that ran it.

The library is a workflow per target agent type, `eval-library-<workflow type>`, created on first
use through the `swarm_eval_library` activity (a workflow cannot update another directly). It
continues-as-new as its history grows, and keeps the newest MAX_SUITES suites and MAX_RUNS runs.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from temporalio import activity, workflow
from temporalio.client import Client, WithStartWorkflowOperation, WorkflowUpdateFailedError
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from examples.swarm_eval.models import Comparison, EvalCase, FailureMode, fingerprint

LIBRARY_WORKFLOW = "EvalLibrary"
LIBRARY_ACTIVITY = "swarm_eval_library"
LIBRARY_TIMEOUT = timedelta(seconds=30)
LIBRARY_RETRY = RetryPolicy(initial_interval=timedelta(milliseconds=200), maximum_interval=timedelta(seconds=2))

MAX_SUITES = 25
MAX_RUNS = 100


def library_workflow_id(target: str) -> str:
    return f"eval-library-{target}"


class Suite(BaseModel):
    """A saved set of cases to run again."""

    name: str
    target: str
    created_at: float
    cases: list[EvalCase]
    contract_hash: str = ""


class CaseOutcome(BaseModel):
    """How one case did in one run."""

    passed: bool
    score: int
    failure_mode: FailureMode


class RunRecord(BaseModel):
    """One finished run, case by case."""

    run_id: str = Field(description="'<dispatcher workflow id>#<n>': the n-th run in that session.")
    suite: str
    suite_hash: str = ""
    evaluator_hash: str = ""
    revision: str = ""
    target: str
    finished_at: float
    models: dict[str, str]
    pass_rate: float
    mean_score: float
    outcomes: dict[str, CaseOutcome]


class LibraryState(BaseModel):
    target: str
    suites: list[Suite] = Field(default_factory=list)
    runs: list[RunRecord] = Field(default_factory=list)
    comparisons: dict[str, Comparison | None] = Field(default_factory=dict)


class SuiteSummary(BaseModel):
    name: str
    cases: int
    created_at: float


class Catalog(BaseModel):
    """What the library holds: its suites, and its runs newest first."""

    suites: list[SuiteSummary]
    runs: list[RunRecord]


def compare(previous: RunRecord, current: RunRecord) -> Comparison:
    """``current`` against ``previous``, over the cases both ran."""
    shared = sorted(set(previous.outcomes) & set(current.outcomes))
    return Comparison(
        previous_run_id=previous.run_id,
        previous_pass_rate=previous.pass_rate,
        pass_rate_delta=round(current.pass_rate - previous.pass_rate, 4),
        fixed=[c for c in shared if current.outcomes[c].passed and not previous.outcomes[c].passed],
        regressed=[c for c in shared if previous.outcomes[c].passed and not current.outcomes[c].passed],
        still_failing=[
            c for c in shared if not previous.outcomes[c].passed and not current.outcomes[c].passed
        ],
    )


@workflow.defn(name=LIBRARY_WORKFLOW)
class EvalLibrary:
    """Saved suites and run records for one target agent type."""

    @workflow.init
    def __init__(self, state: LibraryState) -> None:
        self._state = state

    @workflow.run
    async def run(self, state: LibraryState) -> None:
        await workflow.wait_condition(lambda: workflow.info().is_continue_as_new_suggested())
        await workflow.wait_condition(workflow.all_handlers_finished)
        workflow.continue_as_new(self._state)

    @workflow.update
    def save_suite(self, suite: Suite) -> None:
        """Save an immutable suite. An identical retry is a no-op; changed cases need a new name."""
        existing = self.get_suite(suite.name)
        if existing:
            if fingerprint(existing.model_dump(exclude={"created_at"})) != fingerprint(suite.model_dump(exclude={"created_at"})):
                raise ApplicationError("suite name already exists with different cases or contract", non_retryable=True)
            return
        suites = [s for s in self._state.suites if s.name != suite.name] + [suite]
        self._state.suites = suites[-MAX_SUITES:]

    @workflow.update
    def get_suite(self, name: str) -> Suite | None:
        return next((s for s in self._state.suites if s.name == name), None)

    @workflow.update
    def record_run(self, record: RunRecord) -> Comparison | None:
        """Record a run; return how it compares with the previous run of its suite, if any."""
        existing = next((r for r in self._state.runs if r.run_id == record.run_id), None)
        if existing is not None:
            if existing != record:
                raise ApplicationError("run id already recorded with different evidence", non_retryable=True)
            return self._state.comparisons.get(record.run_id)
        previous = next(
            (r for r in reversed(self._state.runs) if r.suite == record.suite
             and r.suite_hash == record.suite_hash and r.evaluator_hash == record.evaluator_hash), None
        )
        comparison = compare(previous, record) if previous else None
        self._state.runs = (self._state.runs + [record])[-MAX_RUNS:]
        self._state.comparisons[record.run_id] = comparison
        retained = {r.run_id for r in self._state.runs}
        self._state.comparisons = {k: v for k, v in self._state.comparisons.items() if k in retained}
        return comparison

    @workflow.update
    def catalog(self, limit: int) -> Catalog:
        return Catalog(
            suites=[
                SuiteSummary(name=s.name, cases=len(s.cases), created_at=s.created_at)
                for s in reversed(self._state.suites)
            ],
            runs=list(reversed(self._state.runs))[:limit],
        )


LibraryOperation = Literal["save_suite", "get_suite", "record_run", "catalog"]

_RESULT_TYPES: dict[str, Any] = {
    "save_suite": type(None),
    "get_suite": Suite | None,
    "record_run": Comparison | None,
    "catalog": Catalog,
}


class LibraryCall(BaseModel):
    target: str
    operation: LibraryOperation
    argument: Any


class LibraryActivities:
    """The library activity, bound to the worker's client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    @activity.defn(name=LIBRARY_ACTIVITY)
    async def call_library(self, call: LibraryCall) -> Any:
        """One operation on ``call.target``'s library, starting the library if it is new.

        Every operation is an update, reads included, so a library that does not exist yet is
        simply created empty rather than being an error to handle."""
        # Imported here, not at the top: this module also defines a workflow, which the sandbox
        # re-imports, and the client side of the harness has no business there.
        from temporal_agent_harness.harness.agent_client import is_unprocessed_update

        info = activity.info()
        start = WithStartWorkflowOperation(
            LIBRARY_WORKFLOW,
            LibraryState(target=call.target),
            id=library_workflow_id(call.target),
            task_queue=info.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        try:
            result = await self._client.execute_update_with_start_workflow(
                call.operation,
                call.argument,
                start_workflow_operation=start,
                id=f"eval-library:{info.workflow_run_id}:{info.activity_id}",
                result_type=_RESULT_TYPES[call.operation],
            )
        except WorkflowUpdateFailedError as e:
            raise ApplicationError(
                str(e.cause or e), non_retryable=not is_unprocessed_update(e)
            ) from e
        return result.model_dump(mode="json") if isinstance(result, BaseModel) else result


async def call_library(target: str, operation: LibraryOperation, argument: Any) -> Any:
    """From inside a workflow: one library operation, as JSON."""
    if isinstance(argument, BaseModel):
        argument = argument.model_dump(mode="json")
    return await workflow.execute_activity(
        LIBRARY_ACTIVITY,
        LibraryCall(target=target, operation=operation, argument=argument),
        start_to_close_timeout=LIBRARY_TIMEOUT,
        retry_policy=LIBRARY_RETRY,
    )
