# ABOUTME: The shape of a TNH eval case — an ordered CONVERSATION, not a single prompt.
#
# Most eval harnesses model a case as one input and one output, because the thing under test is
# a stateless function call. A harness agent is neither stateless nor single-shot: it is a
# durable session that takes turns. So the natural unit here is a script of messages replayed
# against one freshly-started agent, and the result is every turn it produced.
#
# That is a real capability difference, not a stylistic one. "Book me a flight to Tokyo" ->
# "actually, make it nonstop" -> "confirm it" tests whether the agent carried context across
# turns, which is where conversational agents actually fail, and which a single-prompt dataset
# cannot express at all.

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from temporal_agent_harness.harness.agent_client import TurnResult
from temporal_agent_harness.harness.agent_protocol import AgentConfig, TokenUsage


class TurnStep(BaseModel):
    """One message in a scripted conversation.

    ``function`` names the target ``@agent.accepts`` handler — defaulting to ``ask``, the
    convention every conversational harness agent follows (see the packaged UI's contract in
    ``docs/internal/core-concepts.md``). A non-chat agent (Monty's own ``run_script``, say) just
    names its handler instead, which is what makes this dataset shape agent-agnostic rather than
    chat-specific.

    ``expected`` is free-form: whatever the evaluators for this dataset want to compare against
    for THIS turn. The runner never looks at it — it exists so a multi-turn case can assert on
    an intermediate turn, not only on the final one.
    """

    payload: dict[str, Any]
    function: str = "ask"
    expected: dict[str, Any] | None = None
    labels: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def text(cls, text: str, **kwargs: Any) -> TurnStep:
        """A plain chat message — ``TurnStep.text("book me a flight")``."""
        return cls(payload={"text": text}, **kwargs)


class TurnScript(BaseModel):
    """A whole eval case: which agent to start, and the conversation to run against it.

    Carries the agent's identity (``workflow_type`` + ``task_queue``) rather than assuming a
    single agent under test, so one dataset can compare two agents — or the same agent on two
    task queues running different models — by varying only these fields.
    """

    steps: list[TurnStep]
    workflow_type: str
    task_queue: str
    config: AgentConfig = Field(default_factory=AgentConfig)
    expected: dict[str, Any] | None = None

    @property
    def is_multi_turn(self) -> bool:
        return len(self.steps) > 1


class ScriptResult(BaseModel):
    """Everything one scripted run produced — the object evaluators score.

    ``turns`` holds a full :class:`TurnResult` per step, so an evaluator can reach the events of
    ANY turn, not just the last. That is what makes *process* evaluation possible ("did it search
    before it booked?") rather than only outcome evaluation ("did the final text look right?") —
    and process is usually where an agent actually goes wrong.
    """

    model_config = {"arbitrary_types_allowed": True}

    session_workflow_id: str
    turns: list[TurnResult[Any]] = Field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the script ran to completion and every turn succeeded."""
        return self.error is None and all(turn.ok for turn in self.turns)

    @property
    def final_output(self) -> dict[str, Any]:
        return self.turns[-1].output if self.turns else {}

    @property
    def final_text(self) -> str:
        """The last reply's ``text`` field, for the common chat-shaped agent."""
        value = self.final_output.get("text")
        return value if isinstance(value, str) else ""

    @property
    def trace_ids(self) -> list[str]:
        """One OTel trace id per turn (empty strings when tracing is off).

        The handle for linking this run to a dataset item and hanging scores off it.
        """
        return [turn.otel_trace_id for turn in self.turns]

    @property
    def usage(self) -> TokenUsage:
        """Token usage summed across every turn in the script."""
        totals: dict[str, int | None] = {}
        for turn in self.turns:
            for name, value in turn.usage.model_dump().items():
                if value is None:
                    continue
                totals[name] = (totals.get(name) or 0) + value
        return TokenUsage(**totals)

    def events_of_type(self, event_type: str) -> list[Any]:
        """Every event of ``event_type`` across all turns, in order.

        The workhorse for process evaluators: one call gets you every tool call the agent made
        across a whole conversation.
        """
        return [
            envelope.event
            for turn in self.turns
            for envelope in turn.events
            if envelope.event.type == event_type
        ]


__all__ = ["ScriptResult", "TurnScript", "TurnStep"]
