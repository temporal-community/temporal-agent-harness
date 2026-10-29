"""Worker liveness for the task queues an agent registry points at.

The UI's agent picker used to claim every agent was "Ready" unconditionally, which is a
claim about a *worker*, not about the registry — and the registry is the only thing it
had. Clicking an agent whose worker was down produced a session that started and then
hung: the child workflow's tasks sat on an unpolled queue with nothing to run them.

What the server can tell us is poller presence. ``DescribeTaskQueue`` returns a
``PollerInfo`` per worker long-polling the queue, carrying ``identity`` and
``last_access_time`` — so "is anyone listening" is answerable, cheaply, before we start
anything.

What it deliberately does NOT tell us is which workflow *types* those workers registered.
No field in ``DescribeTaskQueue``, ``WorkerHeartbeat``, or ``WorkerListInfo`` carries them,
because the server never needs to know: dispatch is by task queue and whichever worker
polls first wins, with type resolution happening inside the worker process. That is also
why Temporal requires every worker on a task queue to register the same workflow and
activity types — there is no capability-based routing to fall back on.

Which makes poller presence the right signal rather than a lossy approximation of one. In
a correctly configured deployment, a poller on the queue implies every type declared for
that queue is served, so we are not hedging against a configuration Temporal supports. A
worker polling ``monty-dynamic-agent`` without ``MontyChatAgent`` registered is a
misconfiguration, and detecting *that* needs workers to self-report — a separate concern
from this module, which answers only "is anyone home".
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client

# A live worker re-polls at least once per long-poll timeout (60s), so its ``last_access_time``
# is never older than that by much. The server also keeps poller entries around for a few
# minutes after a worker dies, so presence alone would keep reporting "Ready" at a queue
# nobody is listening to. Two minutes clears the poll interval with enough headroom that a
# briefly slow worker does not flap the chip, while still retiring a dead one promptly.
POLLER_FRESHNESS = timedelta(minutes=2)

# Describing a task queue is a cheap read, but it is on the path of every agent-picker open.
# Cap it so a struggling server degrades the chip to "unknown" instead of hanging the popover.
_DESCRIBE_RPC_TIMEOUT = timedelta(seconds=2)

READY = "ready"
NO_WORKER = "no_worker"
UNKNOWN = "unknown"


@dataclass
class TaskQueueWorkers:
    """Whether anyone is polling one task queue, as of one ``DescribeTaskQueue`` call."""

    task_queue: str
    status: str
    poller_count: int = 0
    # Epoch seconds of the most recent poll across live pollers; None when there are none.
    last_seen: float | None = None
    # Populated only for ``UNKNOWN`` — the describe call itself failed, and the UI says so
    # rather than guessing in either direction.
    error: str | None = None


@dataclass
class RegistryWorkers:
    """Per-task-queue liveness for every queue an agent registry points at."""

    by_task_queue: dict[str, TaskQueueWorkers] = field(default_factory=dict)

    def for_task_queue(self, task_queue: str) -> TaskQueueWorkers:
        return self.by_task_queue.get(
            task_queue,
            TaskQueueWorkers(task_queue=task_queue, status=UNKNOWN, error="Not described."),
        )


async def describe_task_queue_workers(
    temporal: Client,
    task_queues: Iterable[str],
    *,
    freshness: timedelta = POLLER_FRESHNESS,
    now: datetime | None = None,
) -> RegistryWorkers:
    """Describe each distinct task queue concurrently and report who is polling it.

    ``task_queues`` is deduplicated first: agents commonly share a queue (Monty's two
    conversational agents both run on ``monty-dynamic-agent``, which is the supported
    pattern precisely because one worker registers both), and describing it once per agent
    would multiply the RPCs for an identical answer.
    """

    distinct = sorted(set(task_queues))
    if not distinct:
        return RegistryWorkers()

    moment = now or datetime.now(timezone.utc)
    results = await asyncio.gather(
        *(
            _describe_one(temporal, task_queue, freshness=freshness, now=moment)
            for task_queue in distinct
        )
    )
    return RegistryWorkers(by_task_queue={result.task_queue: result for result in results})


async def _describe_one(
    temporal: Client,
    task_queue: str,
    *,
    freshness: timedelta,
    now: datetime,
) -> TaskQueueWorkers:
    request = DescribeTaskQueueRequest(
        namespace=temporal.namespace,
        task_queue=TaskQueue(name=task_queue),
        # Workflow pollers only. An agent that cannot get its workflow tasks run is down as
        # far as the picker is concerned, whatever the activity fleet is doing — and the
        # activity queue may legitimately be served by an entirely different worker.
        task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
    )
    try:
        response = await temporal.workflow_service.describe_task_queue(
            request, timeout=_DESCRIBE_RPC_TIMEOUT
        )
    except Exception as exc:
        # Never fail the agent list over this. The registry is still worth serving; the
        # readiness column just admits it does not know.
        return TaskQueueWorkers(
            task_queue=task_queue,
            status=UNKNOWN,
            error=f"{type(exc).__name__}: {exc}",
        )

    cutoff = now - freshness
    fresh = [
        poller
        for poller in response.pollers
        if poller.HasField("last_access_time")
        and poller.last_access_time.ToDatetime(tzinfo=timezone.utc) >= cutoff
    ]
    if not fresh:
        return TaskQueueWorkers(task_queue=task_queue, status=NO_WORKER)

    last_seen = max(
        poller.last_access_time.ToDatetime(tzinfo=timezone.utc) for poller in fresh
    )
    return TaskQueueWorkers(
        task_queue=task_queue,
        status=READY,
        poller_count=len(fresh),
        last_seen=last_seen.timestamp(),
    )
