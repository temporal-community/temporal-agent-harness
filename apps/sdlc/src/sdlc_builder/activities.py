"""All external I/O lives here or in the workspace tool's activity body."""

import asyncio
import json
from datetime import timedelta

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_workflow import Injected
from temporalio import activity
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig

from .models import Action, ModelRequest, ModelResult


@activity.defn
async def decide(request: ModelRequest) -> ModelResult:
    profile = request.task.profile
    if profile.provider == "demo":
        # Explicitly labelled deterministic exercise; production profiles always call a real model.
        await asyncio.sleep(0.5)
        current_context = request.context
        for index, item in enumerate(request.context):
            if item["role"] == "system" and item["text"].startswith("Message ended"):
                current_context = request.context[index + 1 :]
        if any(
            "Plan declined" in item["text"]
            for item in current_context
            if item["role"] == "you"
        ):
            return ModelResult(
                action=Action(
                    kind="finish",
                    summary="The fixed demo plan was declined. No changes were made. Use a real provider profile for an adaptive planning conversation.",
                )
            )
        step = request.step
        if request.task.mode == "ask":
            action = (
                Action(kind="list", summary="Inspect the demo repository")
                if step == 0
                else Action(
                    kind="finish",
                    summary="This demo contains greeting.py and a unittest. Switch to a provider profile to ask questions about your own repositories.",
                )
            )
        elif step == 0:
            action = Action(kind="list", summary="Find the greeting and its tests")
        elif step == 1:
            action = Action(
                kind="read", path="greeting.py", summary="Read the existing greeting"
            )
        elif step == 2:
            action = Action(
                kind="read",
                path="test_greeting.py",
                summary="Read the expected behavior",
            )
        elif step == 3:
            action = Action(
                kind="plan",
                summary="Make the greeting match its test: Hello, Ada!",
                tasks=[
                    "Inspect greeting and test",
                    "Correct capitalization and punctuation",
                    "Run the unittest",
                    "Review the diff",
                ],
                risks=["This is a fixed demo exercise; only greeting.py will change."],
                completed_tasks=[0],
                active_task=1,
            )
        elif step == 4:
            previous = next(
                json.loads(item["text"])
                for item in reversed(current_context)
                if item["role"] == "tool" and '"path": "greeting.py"' in item["text"]
            )
            action = Action(
                kind="write",
                path="greeting.py",
                expected_hash=previous["hash"],
                content='def greet(name):\n    return f"Hello, {name}!"\n',
                summary="Correct the greeting format",
                completed_tasks=[0, 1],
                active_task=2,
            )
        elif step == 5:
            action = Action(
                kind="check",
                argv=["python3", "-m", "unittest", "-v"],
                summary="Run the greeting unittest",
                completed_tasks=[0, 1, 2],
                active_task=3,
            )
        else:
            result = (
                json.loads(request.context[-1]["text"])
                if request.context[-1]["role"] == "tool"
                else {}
            )
            evidence = (
                "The unittest passed."
                if result.get("exit_code") == 0
                else "The check did not pass or was declined; review its evidence."
            )
            action = Action(
                kind="finish",
                summary=f"Updated greeting.py to return Hello, Ada! {evidence} Review the one-file diff before accepting.",
                active_task=3,
            )
        return ModelResult(action=action)

    # Resolve secrets in the process making the request, never in workflow payloads.
    from .legacy_providers import run_model
    from .store import resolve_credential

    resolving_credential = True
    try:
        key = await asyncio.to_thread(resolve_credential, profile.credential)
        resolving_credential = False
        # Explicit context is bounded by the step budget; no credentials in prompts or errors.
        prompt = json.dumps(
            {
                "mode": request.task.mode,
                "request": request.task.prompt,
                "step": request.step + 1,
                "budget": profile.max_steps,
                "history": request.context,
                "continuation_context": request.task.continuation_context,
            },
            ensure_ascii=False,
        )
        return await run_model(profile, key, prompt)
    except Exception as error:
        from temporalio.exceptions import ApplicationError

        from .failures import FAILURE_TYPE, model_failure

        # SDK exceptions can include request bodies/headers. Keep those out of Temporal history.
        failure = model_failure(error, resolving_credential=resolving_credential)
        raise ApplicationError(
            failure.title,
            failure.model_dump(mode="json"),
            type=FAILURE_TYPE,
            non_retryable=True,
        ) from None


@agent.activity_tool_defn(
    activity_config=ActivityConfig(
        start_to_close_timeout=timedelta(seconds=110),
        retry_policy=RetryPolicy(maximum_attempts=1),
    )
)
async def workspace_action(
    action: Action, task_id: Injected[str], operation_id: Injected[str]
) -> dict:
    """Read, search, edit, or check the task's isolated Git workspace."""
    from .workspaces import perform

    return await perform(task_id, operation_id, action.model_dump())
