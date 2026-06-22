# ABOUTME: Internal harness models for activity-side stream publishing. These are NOT protocol
# wire types (not events, not signal/update/query payloads) — they're plumbing the harness uses
# to let an activity publish stream events back to its workflow's turn. Kept in their own leaf
# module (stdlib + pydantic only, importing nothing else from ``harness``) so BOTH the workflow-
# side runner (``harness/agent_workflow.py``) AND the sandbox-safe activity contracts in
# ``harness/agent_protocol`` (e.g. ``RunSubagentTurnInput``) can embed them without a circular
# import — and without leaking internal machinery into the public ``agent_protocol`` surface.

from __future__ import annotations

from pydantic import BaseModel, Field


class TurnStreamContext(BaseModel):
    """The bits an activity needs to publish stream events back to a specific turn.

    Read on the workflow side from ``AgentWorkflowRunner.current_stream_context``, serialized
    across the activity boundary in the activity's request payload, and unpacked on the activity
    side by ``AgentWorkflowRunner.publisher_from_activity`` (which targets the activity's own
    parent workflow). Callers thread it as one opaque value rather than the individual fields.
    """

    turn_id: str = Field(description="The id of the turn to publish against.")
    turn_number: int = Field(description="The monotonic number of that turn.")
    agent_id: str = Field(
        description="The short id of the agent that owns this turn — stamped onto every event the "
        "activity publishes (``AgentEvent.agent_id``). Threaded here because an activity can't "
        "derive it from ``activity.info()`` (which only knows the workflow_id), and the agent's id "
        "is no longer its workflow_id but a short, parent-assignable handle."
    )
