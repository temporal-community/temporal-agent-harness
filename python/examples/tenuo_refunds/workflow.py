"""A refund support agent whose gated tool calls are checked against a Tenuo warrant.

The agent handles one customer's refund request. Its two tools, ``get_order`` and ``refund``,
run under ``ToolApprovalPolicy.auto_mode()``, and the auto mode evaluator is
``tenuo_evaluator``: each call is checked against the warrant the session was started with
(``start_session.py`` issues one per order). A call the warrant allows runs with no one in the
loop. A call it gates waits until the support lead the warrant names approves it in the worker's
terminal, signing with their key. Anything else is denied, with the reason returned to the
model.

The warrant arrives in the session's init data, so each session carries the authority for its
own task: start one for order A-1001 and the agent can't read or refund A-1002, whatever the
conversation says.

Run it with the shared example stack (session-manager worker + FastAPI/UI); this agent is
registered in ``agents.toml``. See ``README.md``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import Runner, TResponseInputItem

    from temporal_agent_harness.ai_sdks.openai_agents_harness import (
        as_openai_agent_tool,
    )
    from temporal_agent_harness.ai_sdks.tenuo import tenuo_evaluator, warrant_criteria
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from examples.tenuo_refunds import keys
    from examples.tenuo_refunds.issue_warrant import AUTONOMOUS_REFUND_CENTS
    from examples.tenuo_refunds.tools import get_order, refund


TASK_QUEUE = "tenuo-refunds"
DEFAULT_MODEL = "gpt-5.1"

SYSTEM_INSTRUCTION = """\
You are a support agent for an online furniture shop. You handle refund requests.

Look the order up with `get_order` before refunding anything. Refund with `refund`; amounts are
in cents ($30.00 is 3000). A customer can get several partial refunds on one order, up to its
total; you don't need to decide whether a refund is too large, the refund system does that.
If a tool call is denied, tell the customer plainly what you couldn't do and why, and don't
retry the same call. Keep replies short."""


class RefundTask(BaseModel):
    """What the session is for: one customer's refund request, and the warrant scoped to it."""

    warrant: str = Field(
        description="The Tenuo warrant for this refund request, in base64. It names the order "
        "the agent may act on and how much it may refund. `just session A-1001` issues one and "
        "starts the session with it."
    )


def _confirm():
    """``just demo-confirm``: the warrant has no approval gate, so refunds over the autonomous
    limit are confirmed by a click in the chat instead of the support lead's signature."""
    if not keys.CONFIRM_IN_CHAT:
        return ()
    return lambda tool, args: tool == "refund" and args["amount_cents"] > AUTONOMOUS_REFUND_CENTS


@agent.defn(name="TenuoRefundsAgent")
class TenuoRefundsAgentWorkflow:
    """A refund support agent, scoped by the session's Tenuo warrant."""

    @agent.init
    def __init__(self, config: AgentConfig, task: RefundTask) -> None:
        if keys.CONFIG is None:
            raise RuntimeError("Tenuo config not loaded: start this agent with its own worker.py")
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Every call goes to the evaluator: nothing is pre-approved, and the criteria
            # send every tool to the warrant.
            approval_policy_default=ToolApprovalPolicy.auto_mode(),
            auto_approval_criteria_default=warrant_criteria(),
            auto_mode_evaluator=tenuo_evaluator(
                warrant=task.warrant, config=keys.CONFIG, confirm=_confirm()
            ),
        )
        self._conversation: list[TResponseInputItem] = []

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask for a refund, e.g. "Please refund $30 on order A-1001, the desk arrived
        scratched"."""
        sdk_agent = OpenAIAgent(
            name="Refunds",
            instructions=SYSTEM_INSTRUCTION,
            model=DEFAULT_MODEL,
            tools=[
                as_openai_agent_tool(self._runner, get_order),
                as_openai_agent_tool(self._runner, refund),
            ],
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": message.text},
        ]
        result = Runner.run_streamed(sdk_agent, input=input_items, context=self._runner)
        async for _event in result.stream_events():
            pass
        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))
