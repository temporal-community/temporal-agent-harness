"""A typed, interactive agent with no model or external dependencies."""

from typing import Literal

from pydantic import BaseModel, Field
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    MidTurn,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_protocol.events import ReplyDelta
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream


class Settings(BaseModel):
    """Session initialization."""

    prefix: str


class Message(BaseModel):
    """Typed CLI inputs."""

    text: str
    count: int = Field(default=1, gt=0)
    enabled: bool = True
    mode: Literal["fast", "careful"] = "fast"
    tags: list[str] = []
    metadata: dict[str, str] = {}
    profile: str | None = None


class Answer(BaseModel):
    """Public handler response."""

    text: str


@agent.defn(name="CLIExampleAgent")
class CLIExampleAgent:
    @agent.init
    def __init__(self, config: AgentConfig, data: Settings):
        self.prefix = data.prefix
        self.released = False
        self.runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def echo(self, message: Message) -> Answer:
        """Echo a typed message."""
        self.runner.publish(ReplyDelta(text="Echoing…"))
        return Answer(
            text=self.prefix + message.text * message.count
            if message.enabled
            else "off"
        )

    @agent.accepts
    async def fail(self, message: Message) -> Answer:
        """Surface a handler failure."""
        raise ValueError("Synthetic handler failure")

    @agent.accepts
    async def wait_for_release(self, message: Message) -> Answer:
        """Reply when the external client releases the handler."""
        await workflow.wait_condition(lambda: self.released)
        return Answer(text=self.prefix + message.text)

    @workflow.signal
    def release(self) -> None:
        self.released = True
