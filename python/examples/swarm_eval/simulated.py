"""Scripted stand-ins for every model in the eval swarm, and a scripted travel agent to test.

With ``EvalRun.simulate`` the writers, testers and judges call these instead of a model, and the
swarm tests :class:`ScriptedTravelAgent` instead of Monty. That is a run with no API keys and no
cost: for CI, and for trying the swarm (and its console view) before spending anything. Every
choice is drawn from ``workflow.random()``, so a run replays.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel

    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        MidTurn,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from examples.swarm_eval.categories import BY_KEY
    from examples.swarm_eval.models import (
        CaseBatch,
        CaseDraft,
        CriterionResult,
        Exchange,
        Grade,
        TesterMove,
        Verdict,
        WriteCases,
    )

SCRIPTED_TARGET = "ScriptedTravelAgent"

_ROUTES = [("SFO", "SEA"), ("JFK", "LAX"), ("ORD", "DEN"), ("BOS", "MIA"), ("AUS", "PDX")]


async def pause(low: float, high: float) -> None:
    """Stand in for a model call's latency."""
    await workflow.sleep(timedelta(seconds=workflow.random().uniform(low, high)))


async def write_cases(request: WriteCases) -> CaseBatch:
    await pause(0.2, 0.6)
    category = request.definition or BY_KEY[request.category]
    cases = []
    for i in range(request.count):
        origin, destination = _ROUTES[(request.batch * 7 + i) % len(_ROUTES)]
        cases.append(
            CaseDraft(
                persona=f"A traveller testing {category.title.lower()}.",
                goal=f"{category.brief} Route {origin} to {destination} on 2027-03-{10 + i % 18}.",
                opening=f"Hi! I need help: {origin} to {destination} on March {10 + i % 18}.",
                success_criteria=[category.rubric],
            )
        )
    return CaseBatch(cases=cases)


async def next_move(conversation: list[Exchange], max_turns: int) -> TesterMove:
    await pause(0.1, 0.4)
    if len(conversation) >= max_turns or workflow.random().random() < 0.35:
        return TesterMove(done=True)
    return TesterMove(done=False, message="Sounds good, please go ahead.")


async def grade(request: Grade) -> Verdict:
    await pause(0.3, 0.9)
    passed = request.error is None and workflow.random().random() < 0.75
    modes = ["hallucinated_result", "ignored_constraint", "wrong_action", "no_action"]
    return Verdict(
        passed=passed,
        score=workflow.random().randint(4, 5) if passed else workflow.random().randint(1, 3),
        criteria=[
            CriterionResult(criterion=c, met=passed, evidence="scripted")
            for c in request.case.success_criteria
        ],
        failure_mode="none" if passed else ("error" if request.error else workflow.random().choice(modes)),
        summary="Scripted verdict." if passed else "Scripted failure, for trying the swarm.",
    )


class AnyModel(BaseModel):
    """Accepted and ignored: the scripted agent has no model to set."""

    model: str = ""


@agent.tool_defn(inherently_safe=True)
async def search_flights(origin: str, destination: str, date: str) -> str:
    """Pretend to search flights."""
    await pause(0.2, 0.8)
    return f"FL-{origin}{destination}-001 $212 nonstop; FL-{origin}{destination}-002 $180 1 stop"


@agent.defn(name=SCRIPTED_TARGET)
class ScriptedTravelAgent:
    """A travel agent with no model: it 'searches' once per turn and replies from the result."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._turns = 0

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Answer a travel request."""
        self._turns += 1
        found = await self._runner.run_tool(
            f"search-{self._turns}", search_flights, origin="SFO", destination="SEA", date="2027-03-10"
        )
        await pause(0.2, 0.6)
        return TextReply(text=f"I found these options: {found}. Shall I book one?")

    @agent.accepts(mid_turn=MidTurn.ACCEPT, model_callable=False)
    async def set_model(self, message: AnyModel) -> TextReply:
        """Ignore a model choice."""
        return TextReply(text="no model to set")
