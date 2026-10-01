"""A minimal, MODEL-FREE parent agent for the Code Mode policy-hook tests.

Runs scripts through a ``code_mode_tool`` whose host calls are judged by a tiny in-workflow
evaluator (it refuses ``greet`` for "Mallory" and approves everything else), and records every
``HostCallResult`` the tool's ``on_host_call_result`` observer receives. The reply carries both
the script output and the observed results, so a test can assert on what a refusal does to the
script and to the rest of its batch.

Kept in its own module because the Temporal workflow sandbox re-imports a workflow's defining
module, and the test module imports worker-side code the sandbox would reject.

No ``from __future__ import annotations`` — this module defines activity tools whose request/
response models cross Temporal's pydantic converter.
"""

import json
from datetime import timedelta

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel

    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
    from temporal_agent_harness.harness.code_mode import HostCallResult


_ACTIVITY_CONFIG = ActivityConfig(start_to_close_timeout=timedelta(seconds=10))


class AddRequest(BaseModel):
    """Two integers to add."""

    a: int
    b: int


class AddResponse(BaseModel):
    """The sum of two integers."""

    total: int


class GreetRequest(BaseModel):
    """Who to greet."""

    name: str


class GreetResponse(BaseModel):
    """A greeting message."""

    message: str


@agent.activity_tool_defn(name="add", activity_config=_ACTIVITY_CONFIG)
async def add_tool(request: AddRequest) -> AddResponse:
    """Add two integers and return their sum."""
    return AddResponse(total=request.a + request.b)


@agent.activity_tool_defn(name="greet", activity_config=_ACTIVITY_CONFIG)
async def greet_tool(request: GreetRequest) -> GreetResponse:
    """Return a greeting for the given name."""
    return GreetResponse(message=f"hi {request.name}")


POLICY_HOOK_TOOLS = [add_tool, greet_tool]
REFUSED_NAME = "Mallory"
REFUSAL_REASON = "Mallory is not greeted here."


async def _refuse_mallory(ctx: agent.AutoApprovalContext) -> agent.AutoApprovalDecision:
    """Refuse ``greet`` for :data:`REFUSED_NAME`; approve every other host call."""
    request = ctx.tool_input.get("request")
    if ctx.tool_name == "greet" and getattr(request, "name", None) == REFUSED_NAME:
        return agent.AutoApprovalDecision(
            verdict=agent.AutoApprovalVerdict.DENY, reason=REFUSAL_REASON
        )
    return agent.AutoApprovalDecision(verdict=agent.AutoApprovalVerdict.APPROVE)


class RunCode(BaseModel):
    """A script to run in Code Mode."""

    script: str


@agent.defn(name="CodeModePolicyParent")
class CodeModePolicyParentWorkflow:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Every host call goes to the evaluator; the run-code tool itself is pre-approved.
            approval_policy_default=ToolApprovalPolicy.auto_mode(pre_approved_tools=["run_code"]),
            auto_approval_criteria_default=agent.AutoApprovalCriteria(
                sets={"test": agent.AutoApprovalCriteriaSet(effect="Test host call.")},
                default="test",
            ),
            auto_mode_evaluator=_refuse_mallory,
        )
        self._observed: list[HostCallResult] = []
        self._run_code = agent.code_mode_tool(
            POLICY_HOOK_TOOLS, name="run_code", on_host_call_result=self._observed.append
        )

    @agent.accepts
    async def run_code(self, msg: RunCode) -> TextReply:
        """Run a script and reply with its output plus every host-call result observed."""
        output = await self._runner.run_tool("code-1", self._run_code, script=msg.script)
        observed = [
            {
                "tool_id": r.tool_id,
                "tool_name": r.tool_name,
                "tool_input": r.tool_input,
                "result": r.result,
            }
            for r in self._observed
        ]
        return TextReply(text=json.dumps({"output": output, "observed": observed}))
