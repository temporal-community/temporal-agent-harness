"""Workflow-safe models that cross the activity boundary between a Playpen policy and Dogwood.

The workflow side (:class:`~temporal_agent_harness.playpen.policy.DogwoodPolicy`) keeps the
trace of a session's host calls as :class:`DogwoodEvent`\\ s and asks the worker side
(:class:`~temporal_agent_harness.playpen.activity.DogwoodPolicyStore`) to decide each new call
against it, dispatching the activity by NAME so nothing on the workflow path imports the
worker-side module.

No ``from __future__ import annotations`` — these cross Temporal's pydantic converter.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

DOGWOOD_DECIDE_ACTIVITY = "dogwood_decide"

# The event kinds a Playpen trace holds. ``request`` is the call being decided, and stays in the
# trace whatever the verdict, as a record of what was attempted. ``admitted`` marks a call the
# gate let through — by the policy, or by a person who approved it — and ``response`` a call
# that completed, carrying its result. See ``EVENT_SCHEMA`` in the activity module.
EventKind = Literal["request", "admitted", "response"]


class DogwoodEvent(BaseModel):
    """One event in the trace a Dogwood policy decides against.

    ``action`` is the host function's name, ``request_id`` the tool id the call ran under (so
    the request, its admission and its response correlate), and ``timestamp`` workflow time in
    whole seconds. ``input`` is the call's arguments and ``output`` its result, both JSON-native;
    ``output`` is set only on a ``response``."""

    timestamp: int
    action: str
    kind: EventKind
    request_id: str
    input: dict[str, Any]
    output: dict[str, Any] | None = None


class DogwoodDecideInput(BaseModel):
    """Decide ``request`` against the named policy, after ``history``.

    ``policy`` names a directory in the worker's policy store. ``principal`` is the entity id
    the session's calls are made as — every event in one trace shares it."""

    policy: str
    principal: str
    history: list[DogwoodEvent] = Field(default_factory=list)
    request: DogwoodEvent


class DogwoodRule(BaseModel):
    """A policy rule that determined a decision: its position in the policy file and its
    annotations (``@id``, ``@reason``, ``@on_deny``)."""

    index: int
    annotations: dict[str, str] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.annotations.get("id", f"rule {self.index}")


class DogwoodDecision(BaseModel):
    """What the policy decided about one call.

    ``escalate`` means the call was denied only by ``forbid`` rules annotated
    ``@on_deny("escalate")``: a person decides it instead. ``reason`` is written for both the
    person reviewing the call and the model whose script made it. ``rules`` are the rules that
    determined the verdict — empty when nothing permitted the call. ``policy_sha256`` identifies
    the exact policy and schema text the decision was made under."""

    verdict: Literal["allow", "deny", "escalate"]
    reason: str
    rules: list[DogwoodRule] = Field(default_factory=list)
    policy_sha256: str
