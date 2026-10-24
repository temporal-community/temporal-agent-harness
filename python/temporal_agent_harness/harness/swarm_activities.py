# ABOUTME: The harness activity behind every swarm board call — ``swarm_board_update`` sends one
# update to a swarm's board workflow, creating the board first if it does not exist yet
# (update-with-start), so no one has to provision a board before a swarm runs.
#
# Like ``SubagentActivities`` this is a class closing over the worker's ``Client`` (the board is
# another workflow, which an activity reaches only through a client), and ``AgentHarnessPlugin``
# instantiates it from the worker's own client. The board is started on the task queue of the
# worker running the activity, which is why the plugin also registers ``SwarmBoardWorkflow`` on
# every worker it configures.

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from temporalio import activity
from temporalio.client import Client, WithStartWorkflowOperation, WorkflowUpdateFailedError
from temporalio.common import (
    SearchAttributePair,
    TypedSearchAttributes,
    WorkflowIDConflictPolicy,
)
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_protocol import (
    SWARM_BOARD_UPDATE_ACTIVITY,
    SWARM_BOARD_WORKFLOW,
    BoardClaim,
    BoardClaimResult,
    BoardComplete,
    BoardItem,
    BoardOperation,
    BoardPost,
    BoardPostMany,
    BoardPostManyResult,
    BoardRelease,
    BoardState,
    SwarmBoardUpdateInput,
    swarm_board_workflow_id,
)
from temporal_agent_harness.harness.swarm_attributes import AGENT_ROLE, BOARD_ROLE, SWARM_ID

# Each operation's request model and reply model, keyed by its update name.
_OPERATIONS: dict[BoardOperation, tuple[type[BaseModel], type[BaseModel]]] = {
    "post": (BoardPost, BoardItem),
    "post_many": (BoardPostMany, BoardPostManyResult),
    "claim": (BoardClaim, BoardClaimResult),
    "complete": (BoardComplete, BoardItem),
    "release": (BoardRelease, BoardItem),
}


class SwarmActivities:
    """Harness activities for swarms, bound to a Temporal :class:`Client`.

    ``AgentHarnessPlugin`` registers them; build one by hand only when assembling a worker's
    activity list yourself (``activities=[SwarmActivities(client).swarm_board_update]``).
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    @activity.defn(name=SWARM_BOARD_UPDATE_ACTIVITY)
    async def swarm_board_update(self, req: SwarmBoardUpdateInput) -> dict[str, Any]:
        """Apply one operation to one lane of ``req.swarm_id``'s board; return its reply as JSON.

        The update id is this activity's own identity, stable across its retries, so a retried
        claim gets the item the first attempt claimed rather than claiming a second one. A board
        rejection (an unknown item, or one the caller does not hold) fails the activity
        non-retryably with the board's error type. A call the server turns away because the
        board has too many updates in flight fails the attempt, and the activity's retry policy
        tries again (see ``SwarmBoard``)."""
        request_model, reply_model = _OPERATIONS[req.operation]
        argument = request_model.model_validate(req.argument)
        info = activity.info()
        start = WithStartWorkflowOperation(
            SWARM_BOARD_WORKFLOW,
            BoardState(swarm_id=req.swarm_id, lane=req.lane),
            id=swarm_board_workflow_id(req.swarm_id, req.lane),
            task_queue=info.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            search_attributes=TypedSearchAttributes(
                [
                    SearchAttributePair(SWARM_ID, req.swarm_id),
                    SearchAttributePair(AGENT_ROLE, BOARD_ROLE),
                ]
            ),
        )
        try:
            reply = await self._client.execute_update_with_start_workflow(
                req.operation,
                argument,
                start_workflow_operation=start,
                id=f"swarm-board:{info.workflow_run_id}:{info.activity_id}",
                result_type=reply_model,
            )
        except WorkflowUpdateFailedError as e:
            cause = e.cause
            raise ApplicationError(
                str(cause) if cause else "the board rejected the call",
                type=getattr(cause, "type", None) or "BoardRejected",
                non_retryable=True,
            ) from e
        return reply.model_dump(mode="json")
