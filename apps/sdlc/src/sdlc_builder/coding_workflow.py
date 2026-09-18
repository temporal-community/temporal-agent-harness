"""Coding V2: deterministic lifecycle, native role tools, and owned child agents.

V1 remains registered unchanged. Only new tasks explicitly routed here use V2.
Child host calls rendezvous with this controller using durable signals; a single
controller lock serializes workspace operations and meters the entire agent tree.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    import asyncio
    import json
    import shlex

    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from .coding_models import (
        Actor,
        Assignment,
        Blueprint,
        CodingState,
        Evidence,
        HostReply,
        HostRequest,
        Recipe,
        RoleOutput,
        RoleReply,
        ToolCall,
        ToolResult,
    )
    from .coding_tools import coding_tools
    from .coding_workspace import coding_operation, project_merge_operation
    from .conversations import recent_context
    from .failures import FAILURE_TYPE, workflow_failure
    from .integrations import integration_for
    from .models import Control, ConversationState, ConversationTurn, Step, TaskInput
    from .workflow import TASK_QUEUE, SdlcAgentWorkflow

WORKFLOW_NAME_V2 = "SdlcAgentV2"
CHILD_WORKFLOW = "SdlcCodingRoleV2"


def in_scope(path: str, scope: list[str]) -> bool:
    from pathlib import PurePosixPath

    if not path or path.startswith("/") or ".." in PurePosixPath(path).parts:
        return False
    return any(
        p == "." or path == p or (p.endswith("/") and path.startswith(p)) for p in scope
    )


@workflow.defn(name=CHILD_WORKFLOW)
@agent.defn
class CodingRoleWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig):
        self.runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            slash_commands=[],
        )
        self.state = self.runner.state(
            "assignment", Actor(id=config.agent_id, role="starting")
        )
        self.assignment: Assignment | None = None
        self.replies: dict[str, ToolResult] = {}
        self.sequence = 0

    @workflow.run
    async def run(self, _config: AgentConfig):
        await self.runner.run(self)

    @workflow.signal
    def host_reply(self, reply: HostReply):
        self.replies[reply.request_id] = reply.result

    async def dispatch(self, call: ToolCall) -> ToolResult:
        self.sequence += 1
        request_id = f"{self.assignment.id}-{self.sequence}"
        await workflow.get_external_workflow_handle(self.assignment.parent).signal(
            "host_request",
            HostRequest(
                assignment_id=self.assignment.id,
                request_id=request_id,
                caller=workflow.info().workflow_id,
                call=call,
            ),
        )
        await workflow.wait_condition(lambda: request_id in self.replies)
        result = self.replies.pop(request_id)
        with self.state.mutate() as state:
            state.operation = call.kind + (" · " + call.path if call.path else "")
            if call.kind == "model" and result.ok:
                state.model_calls += 1
            elif call.kind == "usage":
                state.input_tokens += call.input_tokens
                state.output_tokens += call.output_tokens
            else:
                state.host_calls += 1
        return result

    @agent.accepts
    async def work(self, assignment: Assignment) -> RoleReply:
        """Execute one controller-scoped assignment and return an assessment."""
        self.assignment = assignment
        with self.state.mutate() as state:
            state.role, state.status = assignment.role, "running"
        try:
            if assignment.profile.provider == "demo":
                output = await demo_role(assignment.role, self.dispatch, self.runner)
            else:
                output = await integration_for(assignment.profile).run_role(
                    assignment.profile,
                    assignment.role,
                    assignment.prompt,
                    coding_tools(
                        self.dispatch, writer=assignment.role == "implementer"
                    ),
                    self.runner,
                    self.dispatch,
                    assignment.max_calls,
                )
            with self.state.mutate() as state:
                state.status, state.operation = "completed", output.summary
            return RoleReply(output=output)
        except Exception as error:
            failure = workflow_failure(error, "model")
            message = (
                str(error)
                if isinstance(error, RuntimeError)
                else failure.title + ". " + failure.explanation
            )
            with self.state.mutate() as state:
                state.status, state.operation = "failed", message[:1000]
            return RoleReply(
                error=message[:2000],
                failure=None if isinstance(error, RuntimeError) else failure,
            )


async def demo_role(role, dispatch, runner) -> RoleOutput:
    # Exercises the same host guards, child workflows and packaged Code Mode,
    # without a network request or pretending that a demo is a model evaluation.
    tools = coding_tools(dispatch, writer=role == "implementer")
    code = tools[-1]
    if role == "implementer":
        script = 'f = await read_file({"path": "greeting.py"})\nfile = f["file"]\nassert file is not None\nawait apply_patch({"changes": [{"path": "greeting.py", "expected_hash": file["hash"], "content": "def greet(name):\\n    return f\\"Hello, {name}!\\"\\n"}]})'
    else:
        script = 'await read_file({"path": "greeting.py"})'
    result = await runner.run_tool(workflow.uuid4().hex, code, script=script)
    if "Script error" in str(result) or "stopped" in str(result):
        raise RuntimeError(str(result))
    return RoleOutput(
        summary="Updated greeting and regression evidence is ready for the controller."
        if role == "implementer"
        else "Inspected the greeting implementation and the supplied check evidence."
    )


@workflow.defn(name=WORKFLOW_NAME_V2)
@agent.defn
class CodingWorkflow(SdlcAgentWorkflow):
    # Reuse only the public conversation/control conventions of V1. Its run_turn
    # and __init__ are never invoked, so historical V1 commands stay unchanged.
    @workflow.init
    def __init__(self, config: AgentConfig):
        self.runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            slash_commands=[],
        )
        self.state = self.runner.state("sdlc", CodingState())
        self.conversation = self.runner.state("conversation", ConversationState())
        self.actionable_failures = self.native_sdk = True
        self.paused = self.cancelled = self.started = False
        self.response = None
        self.current_task = None
        self.context = []
        self.lock = asyncio.Lock()
        self.active_actor = ""
        self.active_role = "coordinator"
        self.role_limit = 0
        self.host_count = 0
        self.receipts: dict[str, tuple[str, ToolResult]] = {}
        self.approved_scope: list[str] = []
        self.child_handles: dict[str, str] = {}
        self.review_reads = 0

    @workflow.run
    async def run(self, _config: AgentConfig):
        await self.runner.run(self)

    @workflow.update
    async def control(self, request: Control) -> dict:
        if (
            request.command == "accept"
            and self.state.current.status == "review"
            and self.current_task.mode == "change"
        ):
            async with self.lock:
                fresh = await self.operation(ToolCall(kind="revision"))
                if not fresh.ok:
                    return {"ok": False, "error": fresh.error}
                self.observe_revision(fresh.revision)
                if (
                    self.state.current.verification != "verified"
                    and not request.text.strip()
                ):
                    return {
                        "ok": False,
                        "error": "Verification is incomplete or stale. Add an acceptance note acknowledging the remaining checks/findings, or send a follow-up to fix them.",
                    }
                with self.state.mutate() as state:
                    state.acceptance_note = request.text
        result = super().control(request)
        if result["ok"] and request.command.startswith("edit_"):
            with self.state.mutate() as state:
                state.verification = "stale"
        return result

    @workflow.update
    async def merge_project(self) -> dict:
        """Durably merge the accepted revision into its configured checkout."""
        async with self.lock:
            current = self.state.current
            if current.status != "accepted":
                return {
                    "ok": False,
                    "error": "Accept the current task revision before merging it",
                }
            if current.mode != "change":
                return {
                    "ok": False,
                    "error": "Read-only tasks have no changes to merge",
                }
            if not current.revision:
                return {
                    "ok": False,
                    "error": "The accepted task has no source revision. Refresh and accept it again before merging.",
                }
            previous = current.project_merge
            if previous and previous.revision == current.revision:
                return previous.model_dump(mode="json")

            accepted_revision = current.revision
            operation_id = workflow.uuid4().hex
            prior_focus, prior_next_action = current.focus, current.next_action
            with self.state.mutate() as state:
                state.status = "merging"
                state.focus = "Merging accepted changes into the project"
                state.next_action = "Waiting for the durable merge activity"
            try:
                result = await workflow.execute_activity(
                    project_merge_operation,
                    args=[
                        self.current_task.task_id,
                        operation_id,
                        accepted_revision,
                    ],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except Exception as error:
                with self.state.mutate() as state:
                    state.status = "accepted"
                    state.focus = prior_focus
                    state.next_action = prior_next_action
                return {
                    "ok": False,
                    "error": "The durable merge activity failed. "
                    + workflow_failure(error, "tool").title,
                }
            if not result.ok or result.receipt is None:
                with self.state.mutate() as state:
                    state.status = "accepted"
                    state.focus = prior_focus
                    state.next_action = prior_next_action
                return {
                    "ok": False,
                    "error": result.error or "The merge activity returned no receipt",
                }
            with self.state.mutate() as state:
                state.status = "accepted"
                state.focus = "Merged into project"
                state.next_action = (
                    "Commit the project changes when ready, or send a follow-up"
                )
                state.project_merge = result.receipt
            return result.receipt.model_dump(mode="json")

    def observe_revision(self, revision: str):
        with self.state.mutate() as state:
            state.revision = revision
            for check in state.checks:
                check.stale = (
                    check.revision != revision or check.after_revision != revision
                )
            if state.review_revision and state.review_revision != revision:
                state.verification = "stale"

    @workflow.signal
    async def host_request(self, request: HostRequest):
        # Caller and assignment are supplied by workflow code, never tool schemas.
        # Cache complete responses to reconcile re-delivery without repeated effects.
        async with self.lock:
            encoded = request.model_dump_json()
            old = self.receipts.get(request.request_id)
            if old:
                result = (
                    old[1]
                    if old[0] == encoded
                    else ToolResult(ok=False, error="Request ID reused")
                )
            elif request.assignment_id != self.active_actor:
                result = ToolResult(ok=False, error="Assignment is no longer active")
            else:
                try:
                    result = await self.host_call(request.call)
                except InterruptedError:
                    result = ToolResult(
                        ok=False, error="Stopped by you; workspace preserved"
                    )
                except Exception as error:
                    result = ToolResult(
                        ok=False, error=workflow_failure(error, "tool").title
                    )
                self.receipts[request.request_id] = (encoded, result)
        await workflow.get_external_workflow_handle(request.caller).signal(
            "host_reply", HostReply(request_id=request.request_id, result=result)
        )

    async def dispatch(self, call: ToolCall) -> ToolResult:
        async with self.lock:
            return await self.host_call(call)

    async def operation(self, call: ToolCall) -> ToolResult:
        return await workflow.execute_activity(
            coding_operation,
            args=[self.current_task.task_id, workflow.uuid4().hex, call],
            start_to_close_timeout=timedelta(seconds=115),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    async def controller_operation(self, call: ToolCall) -> ToolResult:
        # A timed-out script can leave an already-issued host call settling.
        # Controller checks/review must wait for the same writer lock too.
        async with self.lock:
            await self.checkpoint()
            return await self.operation(call)

    async def host_call(self, call: ToolCall) -> ToolResult:
        if call.kind == "usage":
            with self.state.mutate() as state:
                state.input_tokens += call.input_tokens
                state.output_tokens += call.output_tokens
                actor = next(a for a in state.actors if a.id == self.active_actor)
                actor.input_tokens += call.input_tokens
                actor.output_tokens += call.output_tokens
            with self.conversation.mutate() as convo:
                convo.turns[-1].input_tokens += call.input_tokens
                convo.turns[-1].output_tokens += call.output_tokens
            return ToolResult()
        await self.checkpoint()
        actor = next(a for a in self.state.current.actors if a.id == self.active_actor)
        if call.kind == "model":
            used = self.conversation.current.turns[-1].model_calls
            if (
                used >= self.current_task.profile.max_steps
                or actor.model_calls >= self.role_limit
            ):
                return ToolResult(
                    ok=False,
                    error="Model-call budget reached across coordinator and children. Send a follow-up to continue.",
                )
            with self.state.mutate() as state:
                state.model_calls += 1
                next(
                    a for a in state.actors if a.id == self.active_actor
                ).model_calls += 1
                state.focus = f"{self.active_role.title()} is working"
            with self.conversation.mutate() as convo:
                convo.turns[-1].model_calls += 1
            return ToolResult()
        self.host_count += 1
        if self.host_count > 400:
            return ToolResult(
                ok=False, error="400 host-operation limit reached for this message"
            )
        if call.kind in {"patch", "edit"}:
            paths = (
                [c.path for c in call.changes]
                if call.kind == "patch"
                else [call.edit.path]
            )
            if (
                self.current_task.mode != "change"
                or self.active_role != "implementer"
                or not self.approved_scope
            ):
                return ToolResult(
                    ok=False,
                    error="Writes require an approved Change assignment for the implementer",
                )
            if not all(in_scope(p, self.approved_scope) for p in paths):
                return ToolResult(
                    ok=False,
                    error="File is outside the approved blueprint scope; report the blocker for a new approval",
                )
            with self.state.mutate() as state:
                state.verification = (
                    "stale" if state.checks or state.review_revision else "not_run"
                )
                for check in state.checks:
                    check.stale = True
        if call.kind == "check":
            return ToolResult(
                ok=False,
                error="Only the controller can run human-approved verification recipes",
            )
        with self.state.mutate() as state:
            state.host_calls += 1
            actor = next(a for a in state.actors if a.id == self.active_actor)
            actor.host_calls += 1
            actor.operation = call.kind + (" · " + call.path if call.path else "")
            state.focus = f"{self.active_role.title()}: {actor.operation}"
        result = await self.operation(call)
        if (
            self.active_role == "reviewer"
            and call.kind in {"read", "diff"}
            and result.ok
        ):
            self.review_reads += 1
        if result.revision:
            self.observe_revision(result.revision)
        if call.kind in {"patch", "edit"} and result.ok:
            with self.state.mutate() as state:
                state.files_changed = list(
                    dict.fromkeys([*state.files_changed, *result.files])
                )
        self.entry(
            result.operation_id or workflow.uuid4().hex,
            "tool",
            f"{self.active_role}: {call.kind}"
            + (" · " + call.path if call.path else "")
            + (" · " + result.error if result.error else ""),
        )
        return result

    def actor(self, identity, role, limit):
        self.active_actor, self.active_role, self.role_limit = (
            identity,
            role,
            max(1, limit),
        )
        with self.state.mutate() as state:
            state.actors.append(Actor(id=identity, role=role))

    def stage(self, phase: str):
        order = ["inspect", "plan", "build", "check", "review"]
        with self.state.mutate() as state:
            state.phase = phase
            state.focus = {
                "inspect": "Understanding the request",
                "plan": "Review the blueprint",
                "build": "Implementer is working",
                "check": "Running approved checks",
                "review": "Independent review",
            }[phase]
            for step in state.steps:
                step.status = (
                    "completed"
                    if order.index(step.id) < order.index(phase)
                    else "active"
                    if step.id == phase
                    else "pending"
                )

    def remaining(self):
        return (
            self.current_task.profile.max_steps
            - self.conversation.current.turns[-1].model_calls
        )

    async def child(self, role: str, prompt: str, reserve: int = 0) -> RoleOutput:
        await self.checkpoint()
        if self.remaining() <= reserve and self.current_task.profile.provider != "demo":
            raise RuntimeError(
                "Model-call budget reached; insufficient calls remain for independent review. Send a follow-up to continue."
            )
        if role not in self.child_handles:
            self.child_handles[role] = await self.runner.start_subagent(
                role,
                CHILD_WORKFLOW,
                TASK_QUEUE,
                config=AgentConfig(
                    approval_policy=ToolApprovalPolicy.dangerously_skip_all()
                ),
            )
        handle = self.child_handles[role]
        assignment_id = f"{handle}-{workflow.uuid4().hex[:8]}"
        self.actor(assignment_id, role, self.remaining() - reserve)
        reply = RoleReply.model_validate(
            await self.runner.run_subagent_turn(
                handle,
                "work",
                Assignment(
                    id=assignment_id,
                    parent=workflow.info().workflow_id,
                    role=role,
                    profile=self.current_task.profile,
                    prompt=prompt,
                    max_calls=self.role_limit,
                ).model_dump(mode="json"),
            )
        )
        with self.state.mutate() as state:
            next(a for a in state.actors if a.id == assignment_id).status = (
                "failed" if reply.error else "completed"
            )
        await self.checkpoint()
        if reply.error:
            if reply.failure:
                raise ApplicationError(
                    reply.failure.title,
                    reply.failure.model_dump(mode="json"),
                    type=FAILURE_TYPE,
                    non_retryable=True,
                )
            raise RuntimeError(reply.error)
        self.entry(workflow.uuid4().hex, role, reply.output.summary)
        return reply.output

    async def verify(self, blueprint: Blueprint) -> list[Evidence]:
        self.stage("check")
        fresh = await self.controller_operation(ToolCall(kind="revision"))
        if not fresh.ok:
            raise RuntimeError(fresh.error)
        self.observe_revision(fresh.revision)
        if not blueprint.checks:
            return []
        response = await self.gate(
            workflow.uuid4().hex,
            "command",
            "Approve verification commands",
            "Run at the workspace root, on your computer, with a scrubbed environment. These commands may execute repository code.\n\n"
            + "\n".join(shlex.join(c.argv) for c in blueprint.checks)
            + f"\n\nSource revision: {fresh.revision}",
        )
        if not response.approved:
            return []
        with self.state.mutate() as state:
            state.verification = "running"
        evidence = []
        for recipe in blueprint.checks:
            await self.checkpoint()
            result = await self.controller_operation(
                ToolCall(
                    kind="check", argv=recipe.argv, expected_revision=fresh.revision
                )
            )
            receipt = Evidence(
                id=result.operation_id or workflow.uuid4().hex,
                command=shlex.join(recipe.argv),
                exit_code=result.exit_code if result.exit_code is not None else -1,
                output=(result.error or result.output)[:12000],
                revision=result.before_revision,
                after_revision=result.revision,
                stale=result.before_revision != result.revision or not result.ok,
            )
            evidence.append(receipt)
            with self.state.mutate() as state:
                state.checks.append(receipt)
            if result.revision:
                self.observe_revision(result.revision)
            if result.revision != fresh.revision:
                break  # No silently inherited permission for changed scripts/config.
        return evidence

    def begin_turn(self, task: TaskInput, message_id: str) -> list[dict[str, str]]:
        """Transition into a turn while holding the workspace-operation lock."""
        self.current_task = task
        self.paused = self.cancelled = False
        self.response = None
        self.approved_scope = []
        self.host_count = 0
        self.receipts = {}
        history, shortened = recent_context(self.context)
        self.context = history
        with self.conversation.mutate() as convo:
            convo.context_shortened = convo.context_shortened or shortened
            convo.turns.append(
                ConversationTurn(
                    number=self.runner.current_status.current_turn,
                    message_id=message_id,
                    prompt=task.prompt,
                    mode=task.mode,
                    profile_id=task.profile.id,
                    profile_label=task.profile.label,
                    provider=task.profile.provider,
                    model=task.profile.model or "scripted demo",
                    max_steps=task.profile.max_steps,
                )
            )
        with self.state.mutate() as state:
            state.task_id, state.mode, state.model = (
                task.task_id,
                task.mode,
                task.profile.model or "scripted demo",
            )
            state.status, state.phase, state.max_steps = (
                "running",
                "inspect",
                task.profile.max_steps,
            )
            state.gate, state.failure, state.outcome = None, None, ""
            state.actors, state.findings, state.missing_evidence = [], [], []
            state.verification, state.review_revision, state.acceptance_note = (
                "not_run",
                "",
                "",
            )
            state.project_merge = None
            state.repair_attempt = 0
            state.steps = (
                [
                    Step(id=k, title=t)
                    for k, t in [
                        ("inspect", "Understand"),
                        ("plan", "Approve blueprint"),
                        ("build", "Implement"),
                        ("check", "Verify"),
                        ("review", "Independent review"),
                    ]
                ]
                if task.mode == "change"
                else [
                    Step(id="inspect", title="Investigate"),
                    Step(id="review", title="Answer"),
                ]
            )
        return history

    async def run_turn(
        self, task: TaskInput, *, initial: bool, message_id: str = "initial"
    ) -> TextReply:
        # A follow-up admitted while a merge activity is running waits until the
        # accepted revision and its project receipt have settled.
        async with self.lock:
            history = self.begin_turn(task, message_id)
        self.entry(workflow.uuid4().hex, "you", task.prompt)
        try:
            self.stage("inspect")
            self.actor(
                "coordinator-" + workflow.uuid4().hex[:8],
                "coordinator",
                max(1, min(12, self.remaining() - (2 if task.mode == "change" else 0))),
            )
            memory = [
                b.model_dump(mode="json")
                for b in self.state.current.blueprints
                if b.approved
            ]
            prompt = json.dumps(
                {
                    "mode": task.mode,
                    "request": task.prompt,
                    "conversation": history,
                    "accepted_blueprints": memory,
                    "continuation_context": task.continuation_context,
                }
            )
            if task.profile.provider == "demo":
                await demo_role("coordinator", self.dispatch, self.runner)
                plan = RoleOutput(
                    summary="Make the greeting match the regression test.",
                    requirements=["greet('Ada') returns 'Hello, Ada!'"],
                    assumptions=["Preserve the greet(name) interface"],
                    scope=["greeting.py", "test_greeting.py"],
                    checks=[
                        Recipe(
                            title="Greeting regression",
                            argv=["python3", "-m", "unittest", "-v"],
                        )
                    ],
                )
            else:
                plan = await integration_for(task.profile).run_role(
                    task.profile,
                    "coordinator",
                    prompt,
                    coding_tools(self.dispatch, writer=False),
                    self.runner,
                    self.dispatch,
                    self.role_limit,
                )
            with self.state.mutate() as state:
                state.actors[-1].status = "completed"
            if task.mode == "ask":
                return self.finish(plan.summary)
            if not plan.requirements or not plan.scope or len(plan.checks) > 8:
                return self.finish(
                    plan.summary
                    + "\n\nA concrete blueprint with requirements, scope and at most 8 checks is still needed. Send a follow-up to clarify it."
                )
            blueprint = Blueprint(
                revision=len(self.state.current.blueprints) + 1,
                request=task.prompt,
                summary=plan.summary,
                requirements=plan.requirements,
                assumptions=plan.assumptions,
                scope=plan.scope,
                checks=plan.checks,
            )
            with self.state.mutate() as state:
                state.blueprints.append(blueprint)
                state.risks = plan.risks
            self.stage("plan")
            answer = await self.gate(
                workflow.uuid4().hex,
                "plan",
                "Approve this blueprint",
                plan.summary
                + "\n\nRequirements:\n"
                + "\n".join(plan.requirements)
                + "\n\nAssumptions:\n"
                + "\n".join(plan.assumptions)
                + "\n\nWrite scope:\n"
                + "\n".join(plan.scope)
                + "\n\nProposed checks (separate execution approval):\n"
                + "\n".join(shlex.join(c.argv) for c in plan.checks),
            )
            if not answer.approved:
                return self.finish(
                    "Blueprint declined. Send a follow-up with the changes you want. "
                    + answer.text
                )
            with self.state.mutate() as state:
                state.blueprints[-1].approved = True
                state.blueprints[-1].approval_note = answer.text
            blueprint = blueprint.model_copy(
                update={"approved": True, "approval_note": answer.text}
            )
            self.approved_scope = list(plan.scope)
            handoff = {
                "blueprint": blueprint.model_dump(mode="json"),
                "approval_note": answer.text,
            }
            for attempt in range(2):
                with self.state.mutate() as state:
                    state.repair_attempt = attempt
                self.stage("build")
                implementation = await self.child(
                    "implementer", json.dumps(handoff), reserve=2
                )
                evidence = await self.verify(blueprint)
                fresh = await self.controller_operation(ToolCall(kind="revision"))
                if not fresh.ok:
                    raise RuntimeError(fresh.error)
                self.observe_revision(fresh.revision)
                self.stage("review")
                self.review_reads = 0
                review = await self.child(
                    "reviewer",
                    json.dumps(
                        {
                            "blueprint": blueprint.model_dump(mode="json"),
                            "revision": fresh.revision,
                            "checks": [c.model_dump(mode="json") for c in evidence],
                            "instruction": "Inspect the actual diff and source independently; do not assume passing checks cover all requirements.",
                        }
                    ),
                )
                after = await self.controller_operation(ToolCall(kind="revision"))
                if not after.ok:
                    raise RuntimeError(after.error)
                self.observe_revision(after.revision)
                complete = (
                    bool(blueprint.checks)
                    and len(evidence) == len(blueprint.checks)
                    and all(
                        c.exit_code == 0
                        and c.revision == after.revision == c.after_revision
                        for c in evidence
                    )
                )
                verified = (
                    complete
                    and self.review_reads > 0
                    and not implementation.missing_evidence
                    and fresh.revision == after.revision
                    and not review.missing_evidence
                    and not any(
                        f.severity in {"high", "medium"} for f in review.findings
                    )
                )
                with self.state.mutate() as state:
                    state.review_revision = fresh.revision
                    for step in state.steps:
                        if step.id == "review":
                            step.status = (
                                "completed" if self.review_reads else "pending"
                            )
                        elif step.id == "check":
                            step.status = "completed" if complete else "pending"
                    state.findings = review.findings
                    state.missing_evidence = (
                        review.missing_evidence
                        + implementation.missing_evidence
                        + (
                            []
                            if self.review_reads
                            else [
                                "Reviewer returned without inspecting the source or diff."
                            ]
                        )
                        + (
                            []
                            if complete
                            else ["Required checks are missing, failed, or stale."]
                        )
                    )
                    state.verification = "verified" if verified else "incomplete"
                if verified or attempt == 1 or self.remaining() < 3 or not evidence:
                    return self.finish(
                        implementation.summary
                        + "\n\n"
                        + review.summary
                        + (
                            "\n\nChecks and independent review passed for this revision."
                            if verified
                            else "\n\nVerification is incomplete. Review the evidence and findings; send a follow-up or explicitly accept the limitations."
                        )
                    )
                handoff["repair"] = {
                    "checks": [c.model_dump(mode="json") for c in evidence],
                    "review": review.model_dump(mode="json"),
                    "instruction": "One repair attempt within the approved scope. Do not change the requirements or check recipe.",
                }
            raise RuntimeError("Repair limit reached")
        except InterruptedError:
            with self.state.mutate() as state:
                state.status, state.focus, state.gate = (
                    "cancelled",
                    "Stopped at an operation boundary",
                    None,
                )
            return TextReply(text="Stopped; workspace preserved.")
        except Exception as error:
            failure = workflow_failure(
                error, "budget" if "budget" in str(error).lower() else "model"
            )
            with self.state.mutate() as state:
                state.status, state.failure, state.gate = "failed", failure, None
                state.focus = failure.title
                state.next_action = (
                    "Review the evidence, then send a follow-up to continue"
                )
                state.missing_evidence.append(
                    str(error)[:1000]
                    if isinstance(error, RuntimeError)
                    else failure.explanation
                )
            self.entry(
                workflow.uuid4().hex,
                "system",
                failure.title + ". " + failure.explanation,
            )
            return TextReply(text=failure.title)
        finally:
            async with self.lock:
                self.active_actor = ""
                self.approved_scope = []
            # Keep the two owned children idle for follow-ups and late debugger
            # attachment. Closed child WorkflowStreams are not reliably readable
            # in harness 0.4.0. Revoking the assignment above removes authority;
            # closing/deleting the parent terminates its idle children.
            with self.state.mutate() as state:
                for actor in state.actors:
                    if actor.status == "running":
                        actor.status = "stopped"
            with self.conversation.mutate() as convo:
                convo.turns[-1].status, convo.turns[-1].outcome = (
                    self.state.current.status,
                    self.state.current.outcome,
                )
            self.context += [
                {"role": "you", "text": task.prompt},
                self.turn_context_summary(),
            ]

    def finish(self, text: str) -> TextReply:
        with self.state.mutate() as state:
            state.status, state.phase, state.focus = (
                "review",
                "review",
                "Ready for your review",
            )
            state.outcome = text
            state.next_action = (
                "Review the diff, evidence and findings; accept or send a follow-up"
            )
            if state.verification == "not_run" and state.mode == "change":
                state.verification = "incomplete"
        self.entry(workflow.uuid4().hex, "agent", text)
        return TextReply(text=text)
