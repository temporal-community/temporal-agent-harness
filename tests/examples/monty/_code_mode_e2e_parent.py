"""A minimal, MODEL-FREE parent agent used only by the Code Mode end-to-end test.

Its single ``@agent.accepts`` handler runs a script through a ``code_mode_tool`` built over two
tiny deterministic activity tools — so a ``WorkflowEnvironment`` test can exercise the whole Code
Mode stack (stub generation, the sandbox batch loop, host-call dispatch via ``run_tool`` with
argument coercion and result marshalling, and the tool lifecycle events) against real activities,
with no model in the loop.

Kept in its own module (not the test file) because the Temporal workflow sandbox re-imports a
workflow's defining module; the test module imports ``Worker`` / ``Client`` / the Code Mode
activities at top level, which the sandbox would reject. The imports here are the sandbox-safe set.

No ``from __future__ import annotations`` — this module defines activity tools whose request/
response models cross Temporal's pydantic converter, and stringized annotations trip its type-hint
resolution (the same convention every activity-defining module in this repo follows).
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, Field

    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


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


class EchoResponse(BaseModel):
    """A label echoed with a server-side secret appended."""

    value: str


@agent.activity_tool_defn(name="add", activity_config=_ACTIVITY_CONFIG)
async def add_tool(request: AddRequest) -> AddResponse:
    """Add two integers and return their sum."""
    return AddResponse(total=request.a + request.b)


@agent.activity_tool_defn(name="greet", activity_config=_ACTIVITY_CONFIG)
async def greet_tool(request: GreetRequest) -> GreetResponse:
    """Return a greeting for the given name."""
    return GreetResponse(message=f"hi {request.name}")


@agent.activity_tool_defn(name="echo", activity_config=_ACTIVITY_CONFIG)
async def echo_tool(secret: agent.Injected[str], label: str) -> EchoResponse:
    """Echo ``label`` together with a server-side secret. ``secret`` is workflow-injected —
    hidden from the script and supplied by the harness, so the host function is ``echo(label)``."""
    return EchoResponse(value=f"{label}:{secret}")


# The tool set exposed through Code Mode — also what the test worker registers activities for.
CODE_MODE_TOOLS = [add_tool, greet_tool, echo_tool]

# The value the harness injects for echo_tool's Injected[str] `secret` — the script never sees it.
INJECTED_SECRET = "s3cr3t"


class RunCode(BaseModel):
    """A script to run in Code Mode."""

    script: str = Field(description="The Python script to execute in the sandbox.")


@workflow.defn(name="CodeModeE2EParent")
@agent.defn
class CodeModeE2EParentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Skip approvals so the test drives host calls without an approver in the loop.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._run_code = agent.code_mode_tool(
            CODE_MODE_TOOLS, name="run_code", injections={"secret": INJECTED_SECRET}
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def run_code(self, msg: RunCode) -> TextReply:
        """Run a Python script in Code Mode over the add/greet tools and reply with its output."""
        output = await self._runner.run_tool("code-1", self._run_code, script=msg.script)
        return TextReply(text=output)
