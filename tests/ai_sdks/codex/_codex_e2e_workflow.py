# ABOUTME: Agent workflows for the Codex adapter's end-to-end tests. Lives in its own module because
# the tests run under the WORKFLOW SANDBOX, which re-imports the workflow's module -- a pytest test
# module is not safe to import that way.

from __future__ import annotations

import os

from pydantic import BaseModel

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.ai_sdks.codex_harness import CodexSession
    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent import ToolApprovalPolicy
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
    )


class Question(BaseModel):
    """A question for the helper."""

    text: str


class Answer(BaseModel):
    """The helper's answer."""

    text: str


@agent.defn
class EchoChildAgent:
    """A trivial child agent: a parent Codex agent drives it as a subagent."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @agent.accepts
    async def ask(self, q: Question) -> Answer:
        """Answer a question."""
        return Answer(text=f"child says: {q.text}")


@agent.tool_defn(inherently_safe=True)
async def lookup(q: str) -> str:
    """Look up a ticket by id."""
    return f"ticket-{q}: resolved"


@agent.activity_tool_defn(inherently_safe=True)
async def record(note: str) -> str:
    """Record a note in the ledger (a real side effect: the test counts executions)."""
    with open(os.environ["CODEX_TEST_LEDGER"], "a") as f:
        f.write(note + "\n")
    return f"recorded:{note}"


def _new_session() -> CodexSession:
    return CodexSession(tools=[lookup, record], instructions="Use the tools you are given.")


class _CodexAgentBase:
    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the Codex agent something; it may call its tools."""
        result = await self._codex.run(self._runner, message.text)
        return TextReply(text=result.text)


@agent.defn
class CodexAgent(_CodexAgentBase):
    """Codex agent with the harness gate wide open."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._codex = _new_session()


@agent.defn
class GatedCodexAgent(_CodexAgentBase):
    """Codex agent whose tools wait for a human approval."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_human_approval(),
        )
        self._codex = _new_session()


@agent.defn
class CodexParentAgent(_CodexAgentBase):
    """Codex agent that drives EchoChildAgent as a (durable, child-workflow) subagent."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._codex = CodexSession(
            tools=agent.subagent_toolset(
                EchoChildAgent, key="helper", task_queue=workflow.info().task_queue
            ),
            instructions="Delegate to the helper subagent.",
        )
