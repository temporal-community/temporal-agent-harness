from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel
    from typesafe_sdk import Choice, SystemOneResponse
    from temporal_agent_harness.ai_sdks.typesafe_harness import HarnessTypeSafe
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import AgentConfig, ToolApprovalPolicy
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


class Classify(BaseModel):
    """An issue whose scope should be classified."""

    text: str


class Classified(BaseModel):
    """The provider's native typed decision and correlation ID."""

    response: SystemOneResponse
    request_id: str | None = None


@agent.defn(name="TypeSafeHelloAgent")
class TypeSafeHelloAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(config, stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all())
        self._typesafe = HarnessTypeSafe(runner=self._runner)

    @agent.accepts
    async def classify(self, message: Classify) -> Classified:
        """Classify the scope of a repository issue."""
        result = await self._typesafe.system_one(message.text, {
            "scope": Choice(criteria={"low": "A small fix, documentation or tests",
                                      "high": "A feature or substantial change"})})
        return Classified(response=result.response, request_id=result.request_id)
