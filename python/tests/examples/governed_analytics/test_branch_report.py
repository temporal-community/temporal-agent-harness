# ABOUTME: Tests for the BranchReport workflow on generated data, against a stand-in tool policy
# that answers the way the Dogwood verifier does and, as policies/bank.dw does, refuses a public
# report once a recorded result read restricted data: as written, every step runs and the
# report is published; with step 3's join uncommented, the query runs and the run fails at
# the publish, which never runs.

import asyncio
import inspect
import json
import re
import types
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import nexusrpc
import nexusrpc.handler
import pytest
import pytest_asyncio
from temporalio.api.common.v1 import Payload
from temporalio.client import WorkflowFailureError
from temporalio.common import RawValue
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker, WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from examples.governed_analytics.models import PublishedReport
from examples.governed_analytics.sql_analysis import analyze_sql
from examples.governed_analytics.static import workflow as branch_report
from examples.governed_analytics.static.activities import ALL_ACTIVITIES
from examples.governed_analytics.static.workflow import POLICY_ENDPOINT, BranchReport

POLICY_TASK_QUEUE = f"tool-policy-{uuid.uuid4()}"
PUBLISH_REASON = "This session has read restricted data"

_ASKED: list[dict[str, Any]] = []
_RECORDED: list[dict[str, Any]] = []
# The runs that recorded a result read from restricted data.
_READ_RESTRICTED: set[str] = set()


def _verifier_payload(message: str, body: dict[str, Any]) -> RawValue:
    """A payload as the Rust verifiers build it (verifiers/host/src/lib.rs, json_proto_payload)."""
    return RawValue(
        Payload(
            metadata={
                "encoding": b"json/protobuf",
                "messageType": f"temporal_untrusted_workers.toolpolicy.v1.{message}".encode(),
            },
            data=json.dumps(body).encode(),
        )
    )


@nexusrpc.handler.service_handler(name="tool-policy")
class FakeToolPolicy:
    @nexusrpc.handler.sync_operation(name="VerifyToolCalls")
    async def verify(
        self, ctx: nexusrpc.handler.StartOperationContext, request: dict[str, Any]
    ) -> RawValue:
        _ASKED.append(request)
        publishing = request["toolCalls"][0]["name"] == "bank-analyst/publish_report"
        if publishing and request["caller"]["runId"] in _READ_RESTRICTED:
            return _verifier_payload(
                "VerifyToolCallsResponse",
                {"decision": "DECISION_DENIED", "reason": PUBLISH_REASON},
            )
        return _verifier_payload("VerifyToolCallsResponse", {"decision": "DECISION_APPROVED"})

    @nexusrpc.handler.sync_operation(name="RecordInputEvents")
    async def record(
        self, ctx: nexusrpc.handler.StartOperationContext, request: dict[str, Any]
    ) -> RawValue:
        _RECORDED.append(request)
        for event in request["events"]:
            result = event["payloads"][0]["value"]
            if isinstance(result, dict) and result.get("restricted_data"):
                _READ_RESTRICTED.add(request["caller"]["runId"])
        return _verifier_payload("RecordInputEventsResponse", {})


@pytest_asyncio.fixture(scope="module")
async def env() -> AsyncGenerator[WorkflowEnvironment, None]:
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    await env.create_nexus_endpoint(POLICY_ENDPOINT, POLICY_TASK_QUEUE)
    yield env
    await env.shutdown()


@pytest.fixture
def edited_branch_report() -> type[BranchReport]:
    """BranchReport as the demo edits it: workflow.py with its query's `--` lines uncommented."""
    source = inspect.getsource(branch_report)
    edited = re.sub(r"^(\s*)-- ", r"\1", source, flags=re.MULTILINE)
    assert edited != source
    module = types.ModuleType(f"{branch_report.__name__}_edited")
    exec(compile(edited, inspect.getfile(branch_report), "exec"), module.__dict__)
    return module.BranchReport


async def _run(
    env: WorkflowEnvironment, workflow: type[BranchReport], runner: WorkflowRunner
) -> Any:
    """Run the workflow to its end: its result, or why it failed."""
    _ASKED.clear()
    _RECORDED.clear()
    task_queue = f"branch-report-{uuid.uuid4()}"
    # Workers start here rather than in the fixture, which runs on another event loop.
    async with Worker(
        env.client, task_queue=POLICY_TASK_QUEUE, nexus_service_handlers=[FakeToolPolicy()]
    ), Worker(
        env.client,
        task_queue=task_queue,
        workflows=[workflow],
        activities=[*ALL_ACTIVITIES, analyze_sql],
        workflow_runner=runner,
    ):
        handle = await env.client.start_workflow(
            "BranchReport",
            id=f"branch-report-{uuid.uuid4()}",
            task_queue=task_queue,
            result_type=PublishedReport,
        )
        try:
            # A failing workflow task is retried rather than failing the workflow, so a broken
            # workflow would otherwise wait here forever.
            async with asyncio.timeout(10):
                return await handle.result()
        except WorkflowFailureError as e:
            return e.cause


def _asked_actions() -> list[str]:
    return [request["toolCalls"][0]["name"] for request in _ASKED]


def _recorded_results() -> dict[str, Any]:
    return {
        event["name"]: event["payloads"][0]["value"]
        for request in _RECORDED
        for event in request["events"]
    }


STEPS = [
    "bank-analyst/summary_stats",
    "bank-analyst/detect_outliers",
    "bank-analyst/run_sql",
    "bank-analyst/publish_report",
]


async def test_the_report_is_published_when_the_run_reads_only_analytics(
    env: WorkflowEnvironment,
) -> None:
    report = await _run(env, BranchReport, SandboxedWorkflowRunner())

    assert isinstance(report, PublishedReport)
    assert _asked_actions() == STEPS
    assert {request["caller"]["subject"] for request in _ASKED} == {"BranchReport"}
    # Every step's result was recorded, and no query read restricted data.
    results = _recorded_results()
    assert list(results) == [f"{step}/result" for step in STEPS]
    assert not any(results[f"{step}/result"]["restricted_data"] for step in STEPS[:-1])


async def test_uncommenting_the_join_gets_the_report_refused(
    env: WorkflowEnvironment, edited_branch_report: type[BranchReport]
) -> None:
    # Unsandboxed: the sandbox would re-import the edited module by name, and it has no file.
    failure = await _run(env, edited_branch_report, UnsandboxedWorkflowRunner())

    assert isinstance(failure, ApplicationError)
    assert failure.type == "PolicyRefused"
    assert "bank-analyst/publish_report" in str(failure) and PUBLISH_REASON in str(failure)
    # The changed query was allowed and ran; its recorded result says what it read.
    assert _asked_actions() == STEPS
    [query] = [r for r in _ASKED if r["toolCalls"][0]["name"] == "bank-analyst/run_sql"]
    tables = [call["parameters"][0]["value"]["table"] for call in query["toolCalls"][1:]]
    assert tables == ["analytics.branches", "risk.loan_applications"]
    results = _recorded_results()
    assert "loan_default_rate" in results["bank-analyst/run_sql/result"]["columns"]
    assert results["bank-analyst/run_sql/result"]["restricted_data"]
    # The publish never ran.
    assert "bank-analyst/publish_report/result" not in results
