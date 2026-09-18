"""The SDLC controller is itself a harness agent with observable internal state."""

from dataclasses import asdict
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    import json
    import shlex

    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_protocol.events import (
        ModelInteractionEnded,
        ModelInteractionStarted,
        TokenUsage,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from .activities import decide, workspace_action
    from .conversations import recent_context
    from .failures import workflow_failure
    from .integrations import integration_for
    from .models import (
        Check,
        Control,
        ConversationState,
        ConversationTurn,
        Entry,
        FollowUpInput,
        Gate,
        ModelRequest,
        SdlcState,
        SdlcStateV1,
        Step,
        TaskInput,
    )

TASK_QUEUE = "sdlc-builder-v1"
WORKFLOW_NAME = "SdlcAgentV1"


@workflow.defn(name=WORKFLOW_NAME)
@agent.defn
class SdlcAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig):
        # The application gates writes/commands itself, before calling harness tools.
        self.runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            slash_commands=[],
        )
        # Keep historical snapshots/patches byte-for-byte compatible on replay.
        self.actionable_failures = workflow.patched("sdlc-actionable-failures-v1")
        self.native_sdk = workflow.patched("sdlc-native-sdk-v1")
        conversation_enabled = workflow.patched("sdlc-conversation-v1")
        self.state = self.runner.state(
            "sdlc", SdlcState() if self.actionable_failures else SdlcStateV1()
        )
        self.paused = False
        self.cancelled = False
        self.response: Control | None = None
        self.started = False
        self.current_task: TaskInput | None = None
        self.context: list[dict[str, str]] = []
        self.conversation = (
            self.runner.state("conversation", ConversationState())
            if conversation_enabled
            else None
        )

    @workflow.run
    async def run(self, _config: AgentConfig):
        await self.runner.run(self)

    @workflow.query
    def progress(self) -> dict:
        status = self.runner.current_status
        return {
            **asdict(self.state.snapshot()),
            "workflow_id": workflow.info().workflow_id,
            "run_id": workflow.info().run_id,
            **(
                {"conversation": asdict(self.conversation.snapshot())}
                if self.conversation
                else {}
            ),
            "next_turn": status.current_turn + len(status.pending_turns) + 1,
            "can_message": (
                self.started
                and not status.turn_active
                and not status.pending_turns
                and self.state.current.status
                in {"review", "accepted", "failed", "cancelled"}
            ),
        }

    @workflow.update
    def control(self, request: Control) -> dict:
        current = self.state.current
        if request.command == "pause" and current.status == "running":
            self.paused = True
            with self.state.mutate() as state:
                state.focus = "Pause requested; finishing the current action"
        elif request.command == "resume" and self.paused:
            self.paused = False
        elif request.command == "cancel" and current.status in {
            "running",
            "paused",
            "needs_you",
        }:
            self.cancelled = True
            self.paused = False
            with self.state.mutate() as state:
                state.focus = "Stop requested; finishing the current action"
        elif (
            request.command == "respond"
            and current.gate
            and request.gate_id == current.gate.id
            and self.response is None
        ):
            self.response = request
        elif request.command == "accept" and current.status == "review":
            with self.state.mutate() as state:
                state.status = "accepted"
                state.phase = "complete"
                state.focus = "Accepted locally"
                state.next_action = (
                    "Export the patch or send a follow-up below"
                    if self.conversation
                    else "Export the patch or continue with a new task"
                )
                for step in state.steps:
                    if step.status == "active":
                        step.status = "completed"
            if self.conversation:
                with self.conversation.mutate() as conversation:
                    conversation.turns[-1].status = "accepted"
        elif request.command == "edit_started" and current.status in {
            "paused",
            "review",
            "failed",
            "cancelled",
        }:
            # Invalidate before the filesystem write: an interrupted save cannot leave a green check.
            with self.state.mutate() as state:
                for check in state.checks:
                    check.stale = True
                state.next_action = "Manual edits invalidate previous checks; continue to verify changes"
        elif request.command == "edit_finished" and current.status in {
            "paused",
            "review",
            "failed",
            "cancelled",
        }:
            with self.state.mutate() as state:
                if request.text and request.text not in state.files_changed:
                    state.files_changed.append(request.text)
                state.next_action = (
                    "Resume or send a follow-up to verify your edits"
                    if self.conversation
                    else "Resume or continue in a new task to verify your edits"
                )
                state.entries.append(
                    Entry(
                        id=f"manual-{self.state.version}",
                        role="you",
                        text=f"Edited {request.text} in the workspace",
                    )
                )
            if self.conversation:
                self.context.append(
                    {
                        "role": "you",
                        "text": f"I manually edited {request.text}. Re-read it before changing it; earlier check evidence is now stale.",
                    }
                )
        else:
            return {
                "ok": False,
                "error": "This action is no longer available. Refresh and try again.",
            }
        return {"ok": True}

    async def checkpoint(self):
        if self.paused and not self.cancelled:
            with self.state.mutate() as state:
                state.status = "paused"
                state.focus = "Paused at an action boundary"
                state.next_action = "Edit files, then resume when ready"
            await workflow.wait_condition(lambda: not self.paused or self.cancelled)
        if self.cancelled:
            raise InterruptedError("Stopped by you")
        with self.state.mutate() as state:
            state.status = "running"

    async def gate(self, identity: str, kind: str, title: str, detail: str) -> Control:
        self.response = None
        with self.state.mutate() as state:
            state.status = "needs_you"
            state.gate = Gate(id=identity, kind=kind, title=title, detail=detail)
            state.focus = title
            state.next_action = "Waiting for your response"
        await workflow.wait_condition(
            lambda: self.response is not None or self.cancelled
        )
        if self.cancelled:
            raise InterruptedError("Stopped by you")
        response = self.response
        with self.state.mutate() as state:
            state.gate = None
            state.status = "running"
            state.entries.append(
                Entry(
                    id=identity + "-response",
                    role="you",
                    text=response.text
                    or ("Approved" if response.approved else "Declined"),
                )
            )
        return response

    def entry(self, identity: str, role: str, text: str):
        with self.state.mutate() as state:
            state.entries.append(Entry(id=identity, role=role, text=text[:12000]))

    def task_progress(self, action):
        with self.state.mutate() as state:
            for index, step in enumerate(state.steps):
                if index in action.completed_tasks:
                    step.status = "completed"
                elif index == action.active_task:
                    step.status = "active"
                elif step.status == "active":
                    step.status = "pending"

    def turn_context_summary(self) -> dict[str, str]:
        current = self.state.current
        text = f"Message ended with status {current.status}. {current.outcome}"
        failure = getattr(current, "failure", None)
        if failure:
            text += f" Failure ({failure.category}) during {current.phase}: {failure.title}. {failure.explanation}"
        return {"role": "system", "text": text}

    @agent.accepts
    async def build(self, task: TaskInput) -> TextReply:
        """Investigate or implement one development task in its private workspace."""
        if self.started:
            return TextReply(
                text="Each task has one run. Use Continue to create a new task."
            )
        self.started = True
        return await self.run_turn(task, initial=True)

    @agent.accepts
    async def follow_up(self, message: FollowUpInput) -> TextReply:
        """Continue the conversation in this task's existing workspace."""
        if self.current_task is None or self.state.current.status not in {
            "review",
            "accepted",
            "failed",
            "cancelled",
        }:
            return TextReply(
                text="Finish or stop the current message before sending a follow-up."
            )
        # Old tasks retain their original first-turn history. The new handler
        # is their migration boundary: it registers conversation state only now.
        if self.conversation is None:
            prior = self.current_task
            state = self.state.current
            self.context.append(self.turn_context_summary())
            self.conversation = self.runner.state(
                "conversation",
                ConversationState(
                    turns=[
                        ConversationTurn(
                            number=self.runner.current_status.current_turn - 1,
                            message_id="initial",
                            prompt=prior.prompt,
                            mode=prior.mode,
                            profile_id=prior.profile.id,
                            profile_label=prior.profile.label,
                            provider=prior.profile.provider,
                            model=state.model,
                            max_steps=prior.profile.max_steps,
                            model_calls=state.model_calls,
                            input_tokens=state.input_tokens,
                            output_tokens=state.output_tokens,
                            status=state.status,
                            outcome=state.outcome,
                        )
                    ]
                ),
            )
        self.native_sdk = True
        task = TaskInput(
            task_id=self.current_task.task_id,
            prompt=message.prompt,
            mode=message.mode,
            profile=message.profile,
            continuation_context=(
                "This is a follow-up in the SAME task and workspace, which contains prior changes. "
                "Use the conversation history to resolve references and preserve requirements. "
                "Re-read relevant files before editing: earlier hashes and checks may be stale. "
                "Plan approval applies to the current message only; propose and obtain approval "
                "for a new change plan before writes or checks."
            ),
        )
        return await self.run_turn(task, initial=False, message_id=message.message_id)

    async def run_turn(
        self, task: TaskInput, *, initial: bool, message_id: str = "initial"
    ) -> TextReply:
        if initial:
            context = []
            shortened = False
        else:
            context, shortened = recent_context(self.context)
            context.append({"role": "you", "text": task.prompt})
            self.paused = False
            self.cancelled = False
            self.response = None
        self.current_task = task
        self.context = context
        turn_number = self.runner.current_status.current_turn
        prefix = "" if initial else f"turn-{turn_number}-"
        if self.conversation:
            with self.conversation.mutate() as conversation:
                conversation.context_shortened = (
                    conversation.context_shortened or shortened
                )
                conversation.turns.append(
                    ConversationTurn(
                        number=turn_number,
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
        plan_approved = False
        with self.state.mutate() as state:
            state.task_id = task.task_id
            state.mode = task.mode
            state.phase = "inspect"
            state.status = "running"
            state.model = task.profile.model or "scripted demo"
            state.max_steps = task.profile.max_steps
            if not initial:
                state.gate = None
                state.outcome = ""
                state.risks = []
                if self.actionable_failures:
                    state.failure = None
            state.steps = (
                [
                    Step(id=k, title=t)
                    for k, t in [
                        ("inspect", "Understand"),
                        ("plan", "Approve plan"),
                        ("build", "Make changes"),
                        ("check", "Run checks"),
                        ("review", "Review"),
                    ]
                ]
                if task.mode == "change"
                else [
                    Step(id="inspect", title="Investigate"),
                    Step(id="review", title="Answer"),
                ]
            )
        self.entry(prefix + "request", "you", task.prompt)
        operation = "model"
        try:
            for index in range(task.profile.max_steps):
                operation = "model"
                await self.checkpoint()
                identity = f"{prefix}step-{index}"
                with self.state.mutate() as state:
                    state.focus = "Choosing the next action"
                    state.next_action = "Inspect, plan, edit, or verify"
                    state.model_calls += 1
                if self.conversation:
                    with self.conversation.mutate() as conversation:
                        conversation.turns[-1].model_calls += 1
                request = ModelRequest(task=task, context=context, step=index)
                if self.native_sdk and task.profile.provider != "demo":
                    result = await integration_for(task.profile).decide(
                        request, self.runner
                    )
                else:
                    self.runner.publish(
                        ModelInteractionStarted(model=task.profile.model or "demo")
                    )
                    try:
                        result = await workflow.execute_activity(
                            decide,
                            ModelRequest(task=task, context=context, step=index),
                            start_to_close_timeout=timedelta(seconds=150),
                            retry_policy=RetryPolicy(maximum_attempts=1),
                        )
                    except Exception:
                        self.runner.publish(
                            ModelInteractionEnded(model=task.profile.model or "demo")
                        )
                        raise
                    self.runner.publish(
                        ModelInteractionEnded(
                            model=task.profile.model or "demo",
                            usage=TokenUsage(
                                input_tokens=result.input_tokens,
                                output_tokens=result.output_tokens,
                            ),
                        )
                    )
                with self.state.mutate() as state:
                    state.input_tokens += result.input_tokens
                    state.output_tokens += result.output_tokens
                if self.conversation:
                    with self.conversation.mutate() as conversation:
                        conversation.turns[-1].input_tokens += result.input_tokens
                        conversation.turns[-1].output_tokens += result.output_tokens
                action = result.action
                context.append({"role": "agent", "text": action.model_dump_json()})
                self.entry(identity, "agent", action.summary)
                await self.checkpoint()
                with self.state.mutate() as state:
                    state.focus = action.summary
                if action.kind == "finish":
                    self.task_progress(action)
                    with self.state.mutate() as state:
                        state.phase = "review"
                        state.status = "review"
                        state.outcome = action.summary
                        state.next_action = (
                            "Review the answer"
                            if task.mode == "ask"
                            else "Inspect the diff and check evidence, then accept or continue"
                        )
                        for step in state.steps:
                            step.status = (
                                "active" if step.id == "review" else step.status
                            )
                    return TextReply(text=action.summary)
                if action.kind == "plan":
                    with self.state.mutate() as state:
                        state.phase = "plan"
                        state.risks = action.risks
                        state.steps = [
                            Step(id=f"{prefix}plan-{n}", title=title)
                            for n, title in enumerate(action.tasks)
                        ]
                    self.task_progress(action)
                    answer = await self.gate(
                        identity,
                        "plan",
                        "Approve this plan",
                        action.summary + "\n\n" + "\n".join(action.tasks),
                    )
                    plan_approved = answer.approved
                    if plan_approved:
                        with self.state.mutate() as state:
                            state.phase = "build"
                    context.append(
                        {
                            "role": "you",
                            "text": (
                                "Plan approved. "
                                if answer.approved
                                else "Plan declined; revise it. "
                            )
                            + answer.text,
                        }
                    )
                    continue
                if action.kind == "question":
                    answer = await self.gate(
                        identity, "question", "A question for you", action.summary
                    )
                    context.append(
                        {
                            "role": "you",
                            "text": answer.text
                            or "No further information; explain the limitation.",
                        }
                    )
                    continue
                if action.kind in {"write", "check"} and (
                    task.mode == "ask" or not plan_approved
                ):
                    context.append(
                        {
                            "role": "tool",
                            "text": json.dumps(
                                {
                                    "error": "Ask is read-only. Changes/checks require change mode and an approved plan."
                                }
                            ),
                        }
                    )
                    continue
                if action.kind == "check":
                    with self.state.mutate() as state:
                        state.phase = "check"
                    answer = await self.gate(
                        identity,
                        "command",
                        "Allow this command on your computer?",
                        shlex.join(action.argv),
                    )
                    if not answer.approved:
                        context.append(
                            {
                                "role": "tool",
                                "text": json.dumps(
                                    {"error": "User declined command. " + answer.text}
                                ),
                            }
                        )
                        continue
                await self.checkpoint()
                operation = "workspace"
                tool_result = await self.runner.run_tool(
                    identity,
                    workspace_action,
                    action,
                    injections={"task_id": task.task_id, "operation_id": identity},
                )
                context.append(
                    {"role": "tool", "text": json.dumps(tool_result)[:110000]}
                )
                if "error" in tool_result:
                    self.entry(identity + "-error", "tool", tool_result["error"])
                elif action.kind == "write":
                    with self.state.mutate() as state:
                        state.phase = "build"
                        for check in state.checks:
                            check.stale = True
                        if action.path not in state.files_changed:
                            state.files_changed.append(action.path)
                elif action.kind == "check":
                    with self.state.mutate() as state:
                        state.checks.append(
                            Check(
                                id=identity,
                                command=shlex.join(action.argv),
                                exit_code=tool_result["exit_code"],
                                output=tool_result["output"][:12000],
                            )
                        )
                if "error" not in tool_result and tool_result.get("exit_code", 0) == 0:
                    self.task_progress(action)
            operation = "budget"
            raise RuntimeError(
                "The model-call budget was reached. Review the workspace and continue in a new task."
            )
        except InterruptedError:
            with self.state.mutate() as state:
                state.status = "cancelled"
                state.gate = None
                state.focus = "Stopped at an action boundary"
                state.next_action = "Your workspace and diff are preserved"
            return TextReply(text="Stopped; workspace preserved.")
        except Exception as error:
            if self.actionable_failures:
                failure = workflow_failure(error, operation)
                if self.conversation:
                    failure = failure.model_copy(
                        update={
                            "explanation": failure.explanation.replace(
                                "this task's profile", "this message's budget"
                            ),
                            "actions": [
                                action.replace(
                                    "starting the new task", "sending the follow-up"
                                ).replace(
                                    "start a new task from the preserved workspace",
                                    "send a follow-up in this task",
                                )
                                for action in failure.actions
                            ],
                        }
                    )
                    if failure.category == "budget":
                        failure = failure.model_copy(
                            update={
                                "explanation": "The agent used all model calls allowed for this message.",
                                "actions": [
                                    "Review the changes and checks already completed.",
                                    "Send a focused follow-up below. Each message has a fresh model-call budget, adjustable up to 200.",
                                ],
                            }
                        )
                with self.state.mutate() as state:
                    state.status = "failed"
                    state.failure = failure
                    state.focus = failure.title
                    state.gate = None
                    state.next_action = failure.actions[0]
                message = f"{failure.title}. {failure.explanation}\n\n" + "\n".join(
                    failure.actions
                )
                self.entry(prefix + "error", "system", message)
                return TextReply(text=message)
            # Provider failures are already sanitized by the activity; don't copy SDK internals.
            message = (
                str(error)
                if isinstance(error, RuntimeError)
                else "An action failed. Check your provider settings and inspect the harness trace. Your workspace is preserved."
            )
            with self.state.mutate() as state:
                state.status = "failed"
                state.focus = message
                state.gate = None
                state.next_action = "Review the workspace and continue in a new task"
            self.entry(prefix + "error", "system", message)
            return TextReply(text=message)
        finally:
            # Preserve the original request for future messages without changing
            # model-activity inputs in histories recorded before this feature.
            if initial:
                self.context.insert(0, {"role": "you", "text": task.prompt})
            if self.conversation:
                with self.conversation.mutate() as conversation:
                    conversation.turns[-1].status = self.state.current.status
                    conversation.turns[-1].outcome = self.state.current.outcome
                self.context.append(self.turn_context_summary())
