# ABOUTME: What the state layer hands its publisher: a whole-document StateSnapshot at
# registration, and one StatePatch of RFC 6902 ops per committing mutate(). These are the
# layer's own plain dataclasses — deliberately NOT protocol wire types. The runner translates
# them into AgentStateSnapshot / AgentStatePatch stream payloads (agent_protocol/events.py).

"""The contract the state layer hands to the event publisher."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["StateSnapshot", "StatePatch", "StateEvent"]


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """A whole-document snapshot: emitted at registration (version 0) and on request."""

    state_id: str
    version: int
    value: dict[str, Any]  # model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class StatePatch:
    """Emitted once per committing ``mutate()`` or ``set()``."""

    state_id: str
    version: int  # the version this patch produces
    ops: list[dict[str, Any]]  # RFC 6902, applied in order to the version-1 document


StateEvent = StateSnapshot | StatePatch
