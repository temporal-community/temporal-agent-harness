"""A non-travel target with a different message contract and typed init data."""

from pydantic import BaseModel
from temporalio.contrib.workflow_streams import WorkflowStream

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import AgentConfig, ToolApprovalPolicy
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


class IncidentInit(BaseModel):
    """The environment to inspect."""

    environment: str


class Question(BaseModel):
    """A question about a failed workflow."""

    question: str


class Answer(BaseModel):
    """The diagnosis with tool evidence."""

    answer: str


@agent.tool_defn(inherently_safe=True)
async def inspect_failure(environment: str) -> str:
    """Return the test fixture's failure evidence."""
    return f"{environment}: charge timed out"


@agent.defn(name="IncubationTestTarget")
class IncidentAgent:
    @agent.init
    def __init__(self, config: AgentConfig, data: IncidentInit) -> None:
        self._runner = AgentWorkflowRunner(config, stream=WorkflowStream(), approval_policy_default=ToolApprovalPolicy.allow_inherently_safe())
        self.environment = data.environment

    @agent.accepts
    async def investigate(self, question: Question) -> Answer:
        """Diagnose a workflow failure in the configured environment."""
        result = await self._runner.run_tool("inspect", inspect_failure, environment=self.environment)
        return Answer(answer=f"{question.question}: {result}")
