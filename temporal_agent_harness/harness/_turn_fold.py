# ABOUTME: ``TurnFold`` — the pure accumulation of one turn's events into its outcome
# (reply output, terminal error, token usage, trace id).
#
# Two places in the harness read a stream until a turn ends and pull the same four things out
# of it: ``AgentClient.run_turn`` (a caller driving an agent) and
# ``SubagentActivities._consume_child_turn`` (a parent agent driving a child). They differ in
# how they READ — the client merges the whole subagent tree and honours the happens-before
# brackets; the subagent activity deliberately reads only the child's own stream, because an
# activity that gated on a grandchild's turn_end would wedge on a stream it never mounts. That
# difference is load-bearing and must not be unified.
#
# What they share is the fold: given this turn's events in order, what did the turn produce?
# That part is pure — no I/O, no clock, no Temporal — so it lives here, is trivially testable
# against hand-built events, and stays identical on both paths.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from temporal_agent_harness.harness.agent_protocol import (
    AgentEvent,
    AgentEventType,
    TokenUsage,
)

# The TokenUsage fields that are summable across the several model calls a turn makes.
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "thought_tokens",
    "cached_tokens",
    "tool_use_tokens",
    "total_tokens",
)


def _add_usage(total: dict[str, int | None], usage: TokenUsage | None) -> None:
    """Accumulate one model call's usage into ``total``, in place.

    ``None`` means "this provider did not report this number", which is NOT the same as zero —
    so a field stays ``None`` until some call reports it, and only then starts summing. Reporting
    a confident ``0`` for a number nobody measured would quietly understate cost.
    """
    if usage is None:
        return
    for name in _USAGE_FIELDS:
        value = getattr(usage, name, None)
        if value is None:
            continue
        total[name] = (total[name] or 0) + value


@dataclass
class TurnFold:
    """Fold one turn's events into its outcome. Pure — feed events, read the fields.

    Feed every event you see; the fold ignores anything belonging to another turn (a subagent's
    events, or an adjacent turn on the same stream), so a caller can pass its whole stream
    through without pre-filtering. :meth:`feed` returns ``True`` on this turn's ``turn_end``,
    which is the single reliable stop signal — it is published in the runner's ``finally``, so
    it arrives whether the turn replied or raised.
    """

    turn_id: str
    output: dict[str, Any] = field(default_factory=dict)
    got_reply: bool = False
    error: str | None = None
    otel_trace_id: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    model_interactions: int = 0
    _usage: dict[str, int | None] = field(
        default_factory=lambda: dict.fromkeys(_USAGE_FIELDS)
    )

    @property
    def usage(self) -> TokenUsage:
        """Token usage summed over every model interaction in this turn."""
        return TokenUsage(**self._usage)

    def feed(self, envelope: AgentEvent) -> bool:
        """Absorb one event. Returns True once THIS turn has ended."""
        if envelope.turn_id != self.turn_id:
            return False
        event = envelope.event
        match event.type:
            case AgentEventType.TURN_STARTED:
                self.otel_trace_id = event.otel_trace_id
                self.labels = dict(event.labels)
            case AgentEventType.MODEL_INTERACTION_ENDED:
                self.model_interactions += 1
                _add_usage(self._usage, event.usage)
            case AgentEventType.REPLY:
                self.output = event.output
                self.got_reply = True
            case AgentEventType.ERROR:
                self.error = event.message or "agent turn failed"
            case AgentEventType.TURN_END:
                return True
        return False


__all__ = ["TurnFold"]
