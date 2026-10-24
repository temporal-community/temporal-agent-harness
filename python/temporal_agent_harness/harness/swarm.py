# ABOUTME: The swarm board — the shared work list a swarm's agents coordinate through — and the
# workflow-side ways an agent reaches it.
#
#   * ``SwarmBoardWorkflow`` is one LANE of the board: a long-lived workflow (its id derived from
#     the swarm id and the lane name, so every member finds the same one), holding items agents
#     post, claim, complete and release. It is created on first use and continues-as-new as its
#     history grows. A swarm splits its board into lanes because one workflow takes a bounded
#     rate of updates; see ``swarm_board_workflow_id``.
#   * ``SwarmBoard`` is how an agent's own code reaches it from inside the workflow: each call is
#     one ``swarm_board_update`` activity, since a workflow cannot send an update to another.
#   * ``board_toolset()`` wraps the same calls as model-facing tools.
#
# WHY A BOARD AND NOT ONLY PEER MESSAGES: hundreds of agents messaging each other directly is
# O(n²) edges that nobody can see, and a worker has to know whom to ask before it can ask. A
# board is one durable hub: work is posted once, claimed by whoever is free, and every claim
# and completion is recorded against the agent that made it — which is also what lets a swarm
# view draw who worked on what. Peer messages (``runner.ask_peer``) remain for the
# conversations a board cannot express, such as a worker consulting a specialist.

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Any, TypeVar

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_protocol import (
    BOARD_CLAIM_UPDATE,
    BOARD_COMPLETE_UPDATE,
    BOARD_POST_MANY_UPDATE,
    BOARD_POST_UPDATE,
    BOARD_RELEASE_UPDATE,
    BOARD_SNAPSHOT_QUERY,
    DEFAULT_BOARD_LANE,
    SWARM_BOARD_UPDATE_ACTIVITY,
    SWARM_BOARD_WORKFLOW,
    BoardClaim,
    BoardClaimResult,
    BoardComplete,
    BoardItem,
    BoardItemStatus,
    BoardOperation,
    BoardPost,
    BoardPostMany,
    BoardPostManyResult,
    BoardRelease,
    BoardSnapshot,
    BoardState,
    SwarmBoardUpdateInput,
)
from temporal_agent_harness.harness.agent_workflow import (
    _CURRENT_RUNNER,
    AgentWorkflowRunner,
    _enclosing_workflow_instance,
    _runner_of,
    tool_defn,
)

# One board call is a single update round-trip; the bound is generous so a slow server does
# not fail a claim, and short enough that a wedged one surfaces as a tool error.
DEFAULT_BOARD_START_TO_CLOSE_TIMEOUT = timedelta(seconds=30)

# A busy lane turns calls away when it already has the server's limit of updates in flight; that
# clears in milliseconds, so retry quickly rather than at the SDK's default one-second start.
DEFAULT_BOARD_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(milliseconds=100),
    backoff_coefficient=1.5,
    maximum_interval=timedelta(seconds=2),
)

_M = TypeVar("_M", bound=BaseModel)


class _Board:
    """The board's rules, apart from Temporal, so they can be unit-tested directly.

    Items are kept in post order, and a claim takes the OLDEST matching open item — so work is
    served first-come, first-served, and a released item goes back to its original place
    rather than to the end of the line."""

    def __init__(self, state: BoardState) -> None:
        self._swarm_id = state.swarm_id
        self._lane = state.lane
        self._items: dict[str, BoardItem] = {item.id: item for item in state.items}
        self._next_number = state.next_number

    def post(self, req: BoardPost, *, now: float) -> BoardItem:
        item = BoardItem(
            id=f"item-{self._next_number}",
            title=req.title,
            payload=req.payload,
            tags=list(req.tags),
            posted_by=req.posted_by,
            created_at=now,
            updated_at=now,
        )
        self._next_number += 1
        self._items[item.id] = item
        return item

    def post_many(self, req: BoardPostMany, *, now: float) -> BoardPostManyResult:
        return BoardPostManyResult(items=[self.post(post, now=now) for post in req.posts])

    def claim(self, req: BoardClaim, *, now: float) -> BoardClaimResult:
        wanted = set(req.tags)
        for item in self._items.values():
            if item.status is not BoardItemStatus.OPEN:
                continue
            if wanted and wanted.isdisjoint(item.tags):
                continue
            claimed = item.model_copy(
                update={
                    "status": BoardItemStatus.CLAIMED,
                    "claimed_by": req.agent_workflow_id,
                    "attempts": item.attempts + 1,
                    "updated_at": now,
                }
            )
            self._items[item.id] = claimed
            return BoardClaimResult(item=claimed)
        return BoardClaimResult(item=None)

    def check_held(self, item_id: str, agent_workflow_id: str) -> None:
        """Raise unless ``agent_workflow_id`` holds ``item_id`` — the precondition of a
        complete or release, checked in their update validators so a rejected call leaves no
        trace in the board's history."""
        item = self._items.get(item_id)
        if item is None:
            raise ApplicationError(
                f"no board item {item_id!r}", {"item_id": item_id}, type="UnknownBoardItem"
            )
        if item.status is not BoardItemStatus.CLAIMED or item.claimed_by != agent_workflow_id:
            raise ApplicationError(
                f"board item {item_id!r} is {item.status.value}"
                + (f" by {item.claimed_by!r}" if item.claimed_by else "")
                + f", not claimed by {agent_workflow_id!r}",
                {"item_id": item_id, "status": item.status.value, "claimed_by": item.claimed_by},
                type="BoardItemNotHeld",
            )

    def complete(self, req: BoardComplete, *, now: float) -> BoardItem:
        self.check_held(req.item_id, req.agent_workflow_id)
        done = self._items[req.item_id].model_copy(
            update={"status": BoardItemStatus.DONE, "result": req.result, "updated_at": now}
        )
        self._items[req.item_id] = done
        return done

    def release(self, req: BoardRelease, *, now: float) -> BoardItem:
        self.check_held(req.item_id, req.agent_workflow_id)
        reopened = self._items[req.item_id].model_copy(
            update={
                "status": BoardItemStatus.OPEN,
                "claimed_by": None,
                "last_release_reason": req.reason,
                "updated_at": now,
            }
        )
        self._items[req.item_id] = reopened
        return reopened

    def snapshot(self) -> BoardSnapshot:
        counts = {status: 0 for status in BoardItemStatus}
        for item in self._items.values():
            counts[item.status] += 1
        return BoardSnapshot(
            swarm_id=self._swarm_id,
            lane=self._lane,
            items=list(self._items.values()),
            counts=counts,
        )

    def state(self) -> BoardState:
        return BoardState(
            swarm_id=self._swarm_id,
            lane=self._lane,
            items=list(self._items.values()),
            next_number=self._next_number,
        )


@workflow.defn(name=SWARM_BOARD_WORKFLOW)
class SwarmBoardWorkflow:
    """One lane of a swarm's board. Started by the first ``swarm_board_update`` that reaches it.

    Runs until it is sent the ``close`` signal, continuing-as-new (carrying its items) whenever
    Temporal suggests it, so a busy swarm's board never outgrows its history."""

    @workflow.init
    def __init__(self, state: BoardState) -> None:
        self._board = _Board(state)
        self._closed = False

    @workflow.run
    async def run(self, state: BoardState) -> None:
        await workflow.wait_condition(
            lambda: self._closed or workflow.info().is_continue_as_new_suggested()
        )
        await workflow.wait_condition(workflow.all_handlers_finished)
        if not self._closed:
            workflow.continue_as_new(self._board.state())

    @workflow.signal
    def close(self) -> None:
        """Stop the board. A later board call starts a fresh, empty one under the same id."""
        self._closed = True

    @workflow.update(name=BOARD_POST_UPDATE)
    def post(self, req: BoardPost) -> BoardItem:
        return self._board.post(req, now=workflow.time())

    @workflow.update(name=BOARD_POST_MANY_UPDATE)
    def post_many(self, req: BoardPostMany) -> BoardPostManyResult:
        return self._board.post_many(req, now=workflow.time())

    @workflow.update(name=BOARD_CLAIM_UPDATE)
    def claim(self, req: BoardClaim) -> BoardClaimResult:
        return self._board.claim(req, now=workflow.time())

    @workflow.update(name=BOARD_COMPLETE_UPDATE)
    def complete(self, req: BoardComplete) -> BoardItem:
        return self._board.complete(req, now=workflow.time())

    @complete.validator
    def _validate_complete(self, req: BoardComplete) -> None:
        self._board.check_held(req.item_id, req.agent_workflow_id)

    @workflow.update(name=BOARD_RELEASE_UPDATE)
    def release(self, req: BoardRelease) -> BoardItem:
        return self._board.release(req, now=workflow.time())

    @release.validator
    def _validate_release(self, req: BoardRelease) -> None:
        self._board.check_held(req.item_id, req.agent_workflow_id)

    @workflow.query(name=BOARD_SNAPSHOT_QUERY)
    def snapshot(self) -> BoardSnapshot:
        return self._board.snapshot()


class SwarmBoard:
    """A swarm member's handle on one lane of its swarm's board, for use INSIDE its workflow.

    Each call runs one ``swarm_board_update`` activity (registered by ``AgentHarnessPlugin``)
    and is recorded against the calling agent's workflow id. Build one for the running agent
    with :meth:`for_current_agent`, or for any swarm by id::

        board = SwarmBoard.for_current_agent(lane="billing")
        item = await board.claim()
        if item is not None:
            await board.complete(item.id, {"summary": "..."})
    """

    def __init__(
        self,
        swarm_id: str,
        *,
        lane: str = DEFAULT_BOARD_LANE,
        start_to_close_timeout: timedelta = DEFAULT_BOARD_START_TO_CLOSE_TIMEOUT,
        retry_policy: RetryPolicy = DEFAULT_BOARD_RETRY_POLICY,
    ) -> None:
        self.swarm_id = swarm_id
        self.lane = lane
        self._timeout = start_to_close_timeout
        self._retry_policy = retry_policy

    @classmethod
    def for_current_agent(cls, *, lane: str = DEFAULT_BOARD_LANE) -> SwarmBoard:
        """``lane`` of the board of the swarm the agent running this code belongs to.

        Must be called from inside an agent workflow whose runner has a swarm membership — from
        a handler, a ``setup`` hook, or a tool."""
        swarm = _running_agent().swarm
        if swarm is None:
            raise ApplicationError(
                "this agent is not a swarm member (AgentConfig.swarm is unset), so it has no "
                "board",
                type="NotInSwarm",
                non_retryable=True,
            )
        return cls(swarm.swarm_id, lane=lane)

    async def post(
        self,
        title: str,
        payload: dict[str, Any] | None = None,
        *,
        tags: Sequence[str] = (),
    ) -> BoardItem:
        """Post a new OPEN item and return it, with its board-assigned id."""
        return await self._call(
            "post",
            BoardPost(
                title=title,
                payload=payload or {},
                tags=list(tags),
                posted_by=workflow.info().workflow_id,
            ),
            BoardItem,
        )

    async def post_many(
        self, posts: Sequence[tuple[str, dict[str, Any] | None, Sequence[str]]]
    ) -> list[BoardItem]:
        """Post several ``(title, payload, tags)`` items in ONE update, in order — how to seed a
        lane with a backlog without spending an update (and a round-trip) per item."""
        me = workflow.info().workflow_id
        result = await self._call(
            "post_many",
            BoardPostMany(
                posts=[
                    BoardPost(title=title, payload=payload or {}, tags=list(tags), posted_by=me)
                    for title, payload, tags in posts
                ]
            ),
            BoardPostManyResult,
        )
        return result.items

    async def claim(self, *, tags: Sequence[str] = ()) -> BoardItem | None:
        """Claim the oldest OPEN item (carrying any of ``tags``, if given), or ``None``.

        Does not wait for work to appear: a caller with nothing to do decides for itself how
        long to sleep before trying again."""
        result = await self._call(
            "claim",
            BoardClaim(agent_workflow_id=workflow.info().workflow_id, tags=list(tags)),
            BoardClaimResult,
        )
        return result.item

    async def complete(self, item_id: str, result: dict[str, Any] | None = None) -> BoardItem:
        """Mark an item this agent holds DONE, recording ``result``."""
        return await self._call(
            "complete",
            BoardComplete(
                item_id=item_id,
                agent_workflow_id=workflow.info().workflow_id,
                result=result or {},
            ),
            BoardItem,
        )

    async def release(self, item_id: str, reason: str | None = None) -> BoardItem:
        """Hand an item this agent holds back to OPEN, so another agent can take it."""
        return await self._call(
            "release",
            BoardRelease(
                item_id=item_id, agent_workflow_id=workflow.info().workflow_id, reason=reason
            ),
            BoardItem,
        )

    async def _call(self, operation: BoardOperation, argument: BaseModel, result: type[_M]) -> _M:
        output = await workflow.execute_activity(
            SWARM_BOARD_UPDATE_ACTIVITY,
            SwarmBoardUpdateInput(
                swarm_id=self.swarm_id,
                lane=self.lane,
                operation=operation,
                argument=argument.model_dump(mode="json"),
            ),
            start_to_close_timeout=self._timeout,
            retry_policy=self._retry_policy,
            result_type=dict,
        )
        return result.model_validate(output)


def _running_agent() -> AgentWorkflowRunner:
    """The runner of the agent whose workflow is running this code: the one dispatching the
    current tool call if there is one, else the enclosing agent's own."""
    runner = _CURRENT_RUNNER.get()
    if runner is not None:
        return runner
    instance = _enclosing_workflow_instance()
    if instance is None:
        raise RuntimeError("SwarmBoard.for_current_agent() must be called inside an agent workflow")
    return _runner_of(instance)


def board_toolset(*, lane: str = DEFAULT_BOARD_LANE) -> list[Callable[..., Awaitable[Any]]]:
    """One lane of the board as model-facing tools, for the swarm the calling agent belongs to.

    Returns inline ``tool_defn`` tools — ``board_post``, ``board_claim``, ``board_complete`` and
    ``board_release`` — with plain-text arguments a model can fill in reliably. Fold them into an
    agent's tool set like any others; each resolves the board through the running agent's swarm
    membership at call time (see :meth:`SwarmBoard.for_current_agent`)."""

    @tool_defn()
    async def board_post(title: str, details: str = "", tags: list[str] | None = None) -> BoardItem:
        """Post a new piece of work to the swarm's shared board for any agent to claim.

        `title` is a one-line summary, `details` everything the claimer needs, and `tags`
        labels (such as the team or skill required) that claims can filter on."""
        return await SwarmBoard.for_current_agent(lane=lane).post(
            title, {"details": details}, tags=tags or ()
        )

    @tool_defn()
    async def board_claim(tags: list[str] | None = None) -> BoardClaimResult:
        """Claim the oldest open item on the swarm's board, optionally only one carrying any
        of `tags`. The result's `item` is empty when there is no matching work right now."""
        item = await SwarmBoard.for_current_agent(lane=lane).claim(tags=tags or ())
        return BoardClaimResult(item=item)

    @tool_defn()
    async def board_complete(item_id: str, summary: str) -> BoardItem:
        """Mark a board item you claimed as done, with a summary of what you did."""
        return await SwarmBoard.for_current_agent(lane=lane).complete(item_id, {"summary": summary})

    @tool_defn()
    async def board_release(item_id: str, reason: str) -> BoardItem:
        """Give a board item you claimed back to the board for another agent, saying why."""
        return await SwarmBoard.for_current_agent(lane=lane).release(item_id, reason)

    return [board_post, board_claim, board_complete, board_release]


__all__ = [
    "DEFAULT_BOARD_RETRY_POLICY",
    "DEFAULT_BOARD_START_TO_CLOSE_TIMEOUT",
    "SwarmBoard",
    "SwarmBoardWorkflow",
    "board_toolset",
]
