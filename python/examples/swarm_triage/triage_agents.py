"""The swarm triage example's agents: a dispatcher, team leads, workers, specialists, reviewers.

None of them calls a model. Each "thinks" by sleeping a random while and decides by
``workflow.random()``, so a run of hundreds of agents costs nothing, needs no API key, and still
looks like a busy swarm: what is under test is the orchestration, and the view of it.

    TriageDispatcher            posts the backlog to the board, starts everyone else
      ├─ TriageSpecialist × S   peers any worker consults
      ├─ TriageReviewer × R     peers any worker asks for a check
      └─ TriageLead × T         one per team; keeps its worker slots filled
           └─ TriageWorker × W  claims its team's issues, retires after a few, is replaced

Workers reach specialists and reviewers with ``runner.ask_peer``: those agents are the
dispatcher's children, not the worker's, so the worker addresses them by workflow id. Work moves
only through the swarm board, which has one lane per team: a lane is its own workflow, so a
team's claims never queue behind another team's.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        MidTurn,
        SwarmMembership,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from examples.swarm_triage.issues import AREAS, synthetic_issues
    from examples.swarm_triage.models import (
        Advice,
        Consult,
        Review,
        RunTeam,
        ServiceSpec,
        Simulation,
        TeamReport,
        TeamSpec,
        TriageReport,
        TriageRun,
        Verdict,
        Work,
        WorkerSpec,
        WorkReport,
    )

TASK_QUEUE = "swarm-triage"


def _chance(rate: float) -> bool:
    return workflow.random().random() < rate


def _pick(choices: list[str]) -> str:
    return choices[workflow.random().randrange(len(choices))]


async def _think(simulation: Simulation, scale: float = 1.0) -> None:
    low, high = simulation.think_seconds
    await workflow.sleep(timedelta(seconds=workflow.random().uniform(low, high) * scale))


def _workflow_ids(runner: AgentWorkflowRunner, handles: list[str]) -> list[str]:
    by_handle = {s.subagent_id: s.workflow_id for s in runner.current_status.subagents}
    return [by_handle[h] for h in handles]


@agent.defn(name="TriageDispatcher")
class TriageDispatcher:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # The dispatcher runs no tools of its own.
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
            # Started like any session, so it founds a swarm named after itself.
            swarm_default=SwarmMembership(
                swarm_id=workflow.info().workflow_id, role="dispatcher"
            ),
        )

    @agent.accepts
    async def triage(self, msg: TriageRun) -> TriageReport:
        """Triage a synthetic backlog: post it to the swarm board, then start specialists,
        reviewers and one lead per team, and reply once every team has run out of issues."""
        swarm = self._runner.swarm
        assert swarm is not None  # swarm_default guarantees it
        teams = [AREAS[i % len(AREAS)] for i in range(msg.teams)]

        # Seed each team's lane with its share of the backlog, one update per lane.
        issues = synthetic_issues(msg.issues, teams, workflow.random())
        await asyncio.gather(
            *(
                agent.SwarmBoard(swarm.swarm_id, lane=team).post_many(
                    [(i.title, i.payload, [team]) for i in issues if i.team == team]
                )
                for team in teams
                if any(i.team == team for i in issues)
            )
        )

        service = ServiceSpec(simulation=msg.simulation)
        specialists = [
            await self._runner.start_subagent(
                "specialist", "TriageSpecialist", TASK_QUEUE, data=service
            )
            for _ in range(msg.specialists)
        ]
        reviewers = [
            await self._runner.start_subagent("reviewer", "TriageReviewer", TASK_QUEUE, data=service)
            for _ in range(msg.reviewers)
        ]
        leads = [
            await self._runner.start_subagent(
                "lead",
                "TriageLead",
                TASK_QUEUE,
                data=TeamSpec(
                    team=team,
                    workers=msg.workers_per_team,
                    items_per_worker=msg.items_per_worker,
                    specialists=_workflow_ids(self._runner, specialists),
                    reviewers=_workflow_ids(self._runner, reviewers),
                    simulation=msg.simulation,
                ),
            )
            for team in teams
        ]

        outputs = await asyncio.gather(
            *(self._runner.run_subagent_turn(lead, "run_team", {}) for lead in leads)
        )
        reports = [TeamReport.model_validate(out) for out in outputs]
        for handle in [*leads, *specialists, *reviewers]:
            await self._runner.stop_subagent(handle)

        return TriageReport(
            swarm_id=swarm.swarm_id,
            issues=msg.issues,
            done=sum(r.done for r in reports),
            agents_started=len(specialists)
            + len(reviewers)
            + len(leads)
            + sum(r.workers_started for r in reports),
            releases=sum(r.releases for r in reports),
            consults=sum(r.consults for r in reports),
            reviews=sum(r.reviews for r in reports),
            reworks=sum(r.reworks for r in reports),
            wont_fix=sum(r.wont_fix for r in reports),
        )


@agent.defn(name="TriageLead")
class TriageLead:
    @agent.init
    def __init__(self, config: AgentConfig, spec: TeamSpec) -> None:
        self._spec = spec
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )

    @agent.accepts
    async def run_team(self, msg: RunTeam) -> TeamReport:
        """Keep this team's worker slots filled until its issues are gone, then report."""
        reports: list[WorkReport] = []
        started = 0

        async def slot() -> None:
            nonlocal started
            while True:
                worker = await self._runner.start_subagent(
                    "worker",
                    "TriageWorker",
                    TASK_QUEUE,
                    data=WorkerSpec(
                        team=self._spec.team,
                        specialists=self._spec.specialists,
                        reviewers=self._spec.reviewers,
                        simulation=self._spec.simulation,
                    ),
                )
                started += 1
                # Stopped whether its turn returned or failed — but not from a ``finally``, which
                # would also run while an evicted workflow's coroutines are closed, off the
                # workflow's event loop.
                try:
                    out = await self._runner.run_subagent_turn(
                        worker, "work", Work(max_items=self._spec.items_per_worker).model_dump()
                    )
                except Exception:
                    await self._runner.stop_subagent(worker)
                    raise
                await self._runner.stop_subagent(worker)
                report = WorkReport.model_validate(out)
                reports.append(report)
                if report.exhausted:
                    return

        await asyncio.gather(*(slot() for _ in range(self._spec.workers)))
        return TeamReport(
            team=self._spec.team,
            workers_started=started,
            done=sum(r.done for r in reports),
            releases=sum(r.releases for r in reports),
            consults=sum(r.consults for r in reports),
            reviews=sum(r.reviews for r in reports),
            reworks=sum(r.reworks for r in reports),
            wont_fix=sum(r.wont_fix for r in reports),
        )


@agent.tool_defn()
async def close_as_wont_fix(title: str, reason: str) -> str:
    """Close an issue without fixing it. Gated: a human approves it in the console."""
    return f"closed {title!r} as won't-fix: {reason}"


@agent.defn(name="TriageWorker")
class TriageWorker:
    @agent.init
    def __init__(self, config: AgentConfig, spec: WorkerSpec) -> None:
        self._spec = spec
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # close_as_wont_fix is the one tool, and the one call a person should see.
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )

    @agent.accepts
    async def work(self, msg: Work) -> WorkReport:
        """Handle up to ``max_items`` of this team's issues from the swarm board."""
        board = agent.SwarmBoard.for_current_agent(lane=self._spec.team)
        sim = self._spec.simulation
        report = WorkReport()
        while report.done < msg.max_items:
            item = await board.claim()
            if item is None:
                report.exhausted = True
                break
            await _think(sim)

            priority = item.payload.get("priority", "p2")
            if _chance(sim.consult_rate):
                report.consults += 1
                try:
                    advice = Advice.model_validate(
                        await self._runner.ask_peer(
                            _pick(self._spec.specialists),
                            "consult",
                            Consult(title=item.title, area=self._spec.team).model_dump(),
                        )
                    )
                except Exception:  # noqa: BLE001 — any failed consult hands the issue back
                    report.releases += 1
                    await board.release(item.id, "the specialist could not help")
                    continue
                priority = advice.priority

            label = "bug" if "crash" in item.title or "error" in item.title else "task"
            if _chance(sim.approval_rate):
                try:
                    await self._runner.run_tool(
                        f"wont-fix-{item.id}",
                        close_as_wont_fix,
                        title=item.title,
                        reason="out of scope for this team",
                    )
                    label = "wont-fix"
                    report.wont_fix += 1
                except agent.ToolApprovalDenied:
                    pass

            if _chance(sim.review_rate):
                report.reviews += 1
                verdict = Verdict.model_validate(
                    await self._runner.ask_peer(
                        _pick(self._spec.reviewers),
                        "review",
                        Review(title=item.title, label=label, priority=priority).model_dump(),
                    )
                )
                if not verdict.accepted:
                    report.reworks += 1
                    await _think(sim, scale=0.5)

            await board.complete(item.id, {"label": label, "priority": priority})
            report.done += 1
        return report


@agent.defn(name="TriageSpecialist")
class TriageSpecialist:
    @agent.init
    def __init__(self, config: AgentConfig, spec: ServiceSpec) -> None:
        self._sim = spec.simulation
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )

    # ACCEPT: many workers consult at once, and each joins the open turn rather than queuing.
    @agent.accepts(mid_turn=MidTurn.ACCEPT)
    async def consult(self, msg: Consult) -> Advice:
        """Advise on one issue's priority."""
        await _think(self._sim, scale=0.5)
        if _chance(self._sim.error_rate):
            raise RuntimeError(f"no idea about {msg.title!r}")
        return Advice(priority=_pick(["p0", "p1", "p2", "p3"]), note=f"{msg.area} knows this one")


@agent.defn(name="TriageReviewer")
class TriageReviewer:
    @agent.init
    def __init__(self, config: AgentConfig, spec: ServiceSpec) -> None:
        self._sim = spec.simulation
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )

    @agent.accepts(mid_turn=MidTurn.ACCEPT)
    async def review(self, msg: Review) -> Verdict:
        """Check one triaged issue; sometimes send it back for rework."""
        await _think(self._sim, scale=0.3)
        if _chance(self._sim.rework_rate):
            return Verdict(accepted=False, note=f"{msg.priority} looks wrong for {msg.title!r}")
        return Verdict(accepted=True, note="looks right")


ALL_AGENTS = [TriageDispatcher, TriageLead, TriageWorker, TriageSpecialist, TriageReviewer]
