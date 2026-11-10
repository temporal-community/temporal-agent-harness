"""Helpers for wiring Temporal activities into Strands' agent and hook surfaces.

Both ``activity_as_tool`` and ``activity_as_hook`` produce workflow-side objects
that dispatch user activities via :func:`temporalio.workflow.execute_activity`,
so the I/O actually happens off the workflow.
"""

from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

from strands.hooks import BaseHookEvent, HookCallback
from strands.types.tools import AgentTool

from temporalio import workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.workflow import ActivityCancellationType, VersioningIntent

from temporal_agent_harness.ai_sdks.strands_agents._temporal_activity_tool import TemporalActivityTool


def activity_as_tool(
    activity_fn: Callable,
    *,
    task_queue: str | None = None,
    schedule_to_close_timeout: timedelta | None = None,
    schedule_to_start_timeout: timedelta | None = None,
    start_to_close_timeout: timedelta | None = None,
    heartbeat_timeout: timedelta | None = None,
    retry_policy: RetryPolicy | None = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: str | None = None,
    versioning_intent: VersioningIntent | None = None,
    summary: str | None = None,
    priority: Priority = Priority.default,
) -> AgentTool:
    """Wrap a Temporal activity as a Strands tool.

    ``activity_fn`` must be decorated by ``@activity.defn``. It can be a
    function, a method passed as ``Class.method``, or a callable-class activity
    passed as its class. The tool offers the parameters the activity is called
    with: without a method's ``self``, and from ``__call__`` for a class. All
    keyword arguments are forwarded to ``workflow.execute_activity``.

    Raises:
        ValueError: If ``activity_fn`` is not a named activity, or is an
            instance of a callable-class activity; or if it is a class whose
            ``__call__`` is not a function, staticmethod, classmethod or
            ``functools.partialmethod``, can't be read, or takes ``*args``,
            keyword-only arguments or ``**kwargs``.
    """
    options: dict[str, Any] = {
        "task_queue": task_queue,
        "schedule_to_close_timeout": schedule_to_close_timeout,
        "schedule_to_start_timeout": schedule_to_start_timeout,
        "start_to_close_timeout": start_to_close_timeout,
        "heartbeat_timeout": heartbeat_timeout,
        "retry_policy": retry_policy,
        "cancellation_type": cancellation_type,
        "activity_id": activity_id,
        "versioning_intent": versioning_intent,
        "summary": summary,
        "priority": priority,
    }
    return TemporalActivityTool(activity_fn, options)


TEvent = TypeVar("TEvent", bound=BaseHookEvent)


def activity_as_hook(
    activity_fn: Callable,
    *,
    activity_input: Callable[[TEvent], Any],
    task_queue: str | None = None,
    schedule_to_close_timeout: timedelta | None = None,
    schedule_to_start_timeout: timedelta | None = None,
    start_to_close_timeout: timedelta | None = None,
    heartbeat_timeout: timedelta | None = None,
    retry_policy: RetryPolicy | None = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: str | None = None,
    versioning_intent: VersioningIntent | None = None,
    summary: str | None = None,
    priority: Priority = Priority.default,
) -> HookCallback[TEvent]:
    """Wrap a Temporal activity as a Strands hook callback.

    The returned coroutine, when registered with ``HookRegistry.add_callback``,
    dispatches ``activity_fn`` as a Temporal activity each time the associated
    event fires. ``activity_input`` is called with the event to produce a
    serializable activity input — events themselves are not serializable, since
    they hold references to the ``Agent`` and other workflow-bound objects.
    All other keyword arguments are forwarded to ``workflow.execute_activity``.
    """
    options: dict[str, Any] = {
        "task_queue": task_queue,
        "schedule_to_close_timeout": schedule_to_close_timeout,
        "schedule_to_start_timeout": schedule_to_start_timeout,
        "start_to_close_timeout": start_to_close_timeout,
        "heartbeat_timeout": heartbeat_timeout,
        "retry_policy": retry_policy,
        "cancellation_type": cancellation_type,
        "activity_id": activity_id,
        "versioning_intent": versioning_intent,
        "summary": summary,
        "priority": priority,
    }

    async def callback(event: TEvent) -> None:
        await workflow.execute_activity(activity_fn, activity_input(event), **options)

    return callback
